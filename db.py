import sqlite3
import io
import numpy as np
import torch
import glob
import sys
import os

from collections import defaultdict

from utils.processImage import ImageProcessor, Rescale, RandomCrop, ToTensor
from utils.const import DATA_DIR, IMG_SIZE, BATCH_SIZE

from sklearn.preprocessing import OneHotEncoder

from PIL import Image

import torchvision.transforms as transforms
from torch.utils.data import Dataset


class FoxDatabaseFormatting(Dataset):
    """ Collates information fox images stored locally for database insertion """
    def __init__(self):
        
        self.classes = os.listdir(DATA_DIR)
        
        one_hot = OneHotEncoder(sparse_output=True)
        one_hot.fit(np.array(self.classes).reshape(-1, 1))
        
        feature_encodings = one_hot.transform(np.array(self.classes).reshape(-1, 1)).toarray()
        feature_names = one_hot.get_feature_names_out()
        
        class_map = defaultdict(list)
        
        for i in range(len(self.classes)):
            class_name = feature_names[i].split('_')[1]
            class_map[class_name] = feature_encodings[i]
            
        self.class_map = class_map
        
        self.data = []
        
        self.transform = transforms.Compose(
            [
                Rescale((IMG_SIZE, IMG_SIZE)),
                # RandomCrop(IMG_SIZE),
                ToTensor()
            ]
        )
    
        file_list = glob.glob(DATA_DIR + '/*')
        for class_path in file_list:
            if sys.platform == 'win32':
                class_name = class_path.split('\\')[-1]
            #  unix systems index their files differently
            else:
                class_name = class_path.split('/')[-1]
            for img_path in glob.glob(class_path + '/*.jpg'):
                self.data.append([img_path, class_name])
        
    def __len__(self):
        return len(self.data)
        
    def __getitem__(self, key):
        if torch.is_tensor(key):
            key = key.tolist()
        img_path, class_name = self.data[key]
        
        #  dtype must be float32 otherwise Conv2d will complain
        rgb_mat = np.array(Image.open(img_path).convert('RGB'), dtype=np.float32)
        
        class_id = self.class_map[class_name] 
        
        rgb_mat = self.transform(rgb_mat)
            
        return class_id, class_name, rgb_mat


class FoxDB:
    """ Handles manipulation of fox database """
    def __init__(self):
        self.conn = sqlite3.connect(
            'foxes.db', 
            detect_types=sqlite3.PARSE_DECLTYPES,
            autocommit=True
            )
        self.cursor = self.conn.cursor()
        
        self.dataset = FoxDatabaseFormatting()
       
        self.img_process = ImageProcessor(
            batch_size=BATCH_SIZE,
            img_size=IMG_SIZE
        )
        
    def _adapt_array(self, arr):
        """Saves the array to a binary file in NumPy .npy format, 
        and then reads the bytes, and then stores them as a 
        sqlite3 binary data type so that it can be written into the 
        SQL database.

        Args:
            arr (numpy.ndarray): numpy array of data

        Returns:
            sqlite.Binary: binary data
        """
        
        out = io.BytesIO()
        np.save(out, arr)
        out.seek(0)
        return sqlite3.Binary(out.read())
    
    def _convert_array(self, text):
        """Reads the bytes stored in the SQL database, and then converts 
        them back into a numpy array.
        
        Args:
            text (str): the text
            
        Returns:
            numpy.ndarray: array of converted data
        """
        
        out = io.BytesIO(text)
        out.seek(0)
        return np.load(out)
    
    def create_fox_train(self):
        """ Creates table if one does not already exist. The table 
        has the form class_name-i as the primary key, where i is an integer
        number, and class_name is the name of the class (e.g. 'red-fox-423'). 
        
        The class_id is the one-hot encoding of the class names. It will not 
        be viewable from DB browser since it is an array and has to be stored 
        as a blob. 
        
        Similarly for rgb_mat, which is the RGB matrix of the image,
        this is a numpy.ndarray object of dtype float32, and thus will also be 
        stored as a blob in the DB browser. """
        
        self.cursor.execute(
        """--sql
        CREATE TABLE IF NOT EXISTS Foxes (
            img_id TEXT PRIMARY KEY,
            class_id ARRAY,
            class_name TEXT,
            rgb_mat ARRAY
            );
        """
        )
        
    def drop_table(self):
        """ Deletes the table """
        self.cursor.execute(
        """--sql
        DROP TABLE IF EXISTS Foxes;
        """
        )
        
    def insert_fox_train(self):
        """ Inserts entire fox training dataset into SQL database 
        by converting the RGB matrices into binary data, and then 
        using our custom function to push non-text data into the 
        database """
        
        #  registers np.ndarray as a type that can be inserted into the database
        sqlite3.register_adapter(np.ndarray, self._adapt_array)
        
        #  counter for how many class_names we have
        #  will be used for img_id later
        class_counts = defaultdict(int)
        
        for sample in enumerate(self.dataset):
            class_id = sample[1][0]
            
            #  string representation of class
            class_name = sample[1][1]
            
            #  increment class counter for img_id
            class_counts[class_name] += 1
            
            #  need to do this since sqlite3 doesn't support torch.Tensor objects
            rgb_mat = sample[1][2].detach().numpy()
            
            #  the img_id primary key is given by the class_name plus the instance number
            img_id = class_name + '-' + str(class_counts[class_name])
            
            print(f'Inserting {img_id}')
            
            self.cursor.execute(
                """--sql
                INSERT INTO Foxes (img_id, class_id, class_name, rgb_mat) 
                VALUES (?, ?, ?, ?);
                """, (img_id, class_id, class_name, rgb_mat)
                )
            
    def read_fox_train(self):
        """ Reads fox training data from SQL database, and then converts it back into a numpy array
        
        Returns:
            numpy.ndarray: fox dataset
        """
        #  registers array as a type using our custom function
        sqlite3.register_converter('array', self._convert_array)
        
        self.cursor.execute(
        """--sql
        SELECT rgb_mat, class_id FROM Foxes;
        """
        )
        rows = self.cursor.fetchall()
        return rows
    
    def get_length(self):
        """Returns the length of SQL database
        
        Returns:
            int: length of SQL database
        """
        self.cursor.execute(
        """--sql
        SELECT COUNT(1) FROM Foxes
        """
        )
        count = self.cursor.fetchall()
        return count
    
    def format_fox_train(self):
        """Converts output of read_fox_train() to tensors, so that it 
        can be used in CNN.
        
        Returns:
            tuple(torch.Tensor, torch.Tensor): inputs, labels in that order
        """
        rows = self.read_fox_train()
        inputs, labels = rows
        
        #  dtype has to be float32 otherwise Conv2d will complain
        inputs = np.array(inputs, dtype=np.float32)
        #  labels needs to be tensor otherwise DataLoader will get upset
        labels = torch.tensor(labels)
        
        return inputs, labels


if __name__ == '__main__':
    format = FoxDatabaseFormatting()
    
    db = FoxDB()
    
    db.drop_table()
    db.create_fox_train()
    db.insert_fox_train()
    
    #  to test if the values were inserted properly
    values = db.read_fox_train()
    
    print(np.array(values).shape)
    