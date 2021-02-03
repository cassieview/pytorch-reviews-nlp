import os
import argparse
from pathlib import Path
from azureml.core.run import Run
import torch
from torchtext.data.utils import ngrams_iterator
from torchtext.data.utils import get_tokenizer
from torchtext.datasets import text_classification
import pandas as pd
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
import time
from torch.utils.data.dataset import random_split
from torchtext.data.utils import ngrams_iterator, get_tokenizer
import torch

from .data import setup_datasets


###################################################################
# Helpers                                                         #
###################################################################
def check_dir(path, check=False):
    if check:
        assert os.path.exists(path), '{} does not exist!'.format(path)
    else:
        if not os.path.exists(path):
            os.makedirs(path)
        return Path(path).resolve()

def info(msg, char = "#", width = 75):
    print("")
    print(char * width)
    print(char + "   %0*s" % ((-1*width)+5, msg) + char)
    print(char * width)

def download():
    target = './.data/yelp_review_full_csv'
    if not os.path.exists(target):
        print('downloading {} ...'.format(target))
        # check directory for data if it doesnt already exist
        if not os.path.isdir('./.data'):
            os.mkdir('./.data')
        #Get train and text dataset to tensor
        yelp_train_dataset, yelp_test_dataset = text_classification.DATASETS['YelpReviewFull'](
            root='./.data', ngrams=2, vocab=None)

        print(f'labels: {yelp_train_dataset.get_labels()}')
        return yelp_train_dataset, yelp_test_dataset
    else:
        print('{} already exists, skipping step'.format(str(target)))
        train_csv_file = "./.data/yelp_review_full_csv/train.csv"
        test_csv_file = "./.data/yelp_review_full_csv/test.csv"
        yelp_train_dataset, yelp_test_dataset  = setup_datasets(train_csv_file, test_csv_file, ngrams=2)
        return yelp_train_dataset, yelp_test_dataset 

def addGender(df):
    if df['label'] >= 3:
        return 'F'
    else:
        return 'M'
## TODO: use new dataframe stuff here?

def get_df():
    #File path to the csv file
    csv_file = "./.data/yelp_review_full_csv/train.csv"

    # Read csv file into dataframe
    df = pd.read_csv(csv_file, names=["label", "review"])
    df['gender'] = df.apply(addGender, axis=1)
    # Print first 5 rows in the dataframe
    print(df.head())
    print(df['label'].value_counts())
    print(df['gender'].value_counts())
    return df

def generate_batch(batch):
    label = torch.tensor([entry[0] for entry in batch])
    text = [entry[1] for entry in batch]
    offsets = [0] + [len(entry) for entry in text]
    # torch.Tensor.cumsum returns the cumulative sum
    # of elements in the dimension dim.
    # torch.Tensor([1.0, 2.0, 3.0]).cumsum(dim=0)

    offsets = torch.tensor(offsets[:-1]).cumsum(dim=0)
    text = torch.cat(text)
    return text, offsets, label

###################################################################
# Training                                                        #
###################################################################

class TextSentiment(nn.Module):
    def __init__(self, vocab_size, embed_dim, num_class):
        super().__init__()
        self.embedding = nn.EmbeddingBag(vocab_size, embed_dim, sparse=True)
        self.fc = nn.Linear(embed_dim, num_class)
        self.init_weights()
    def init_weights(self):
        initrange = 0.5
        self.embedding.weight.data.uniform_(-initrange, initrange)
        self.fc.weight.data.uniform_(-initrange, initrange)
        self.fc.bias.data.zero_()
    def forward(self, text, offsets):
        embedded = self.embedding(text, offsets)
        return self.fc(embedded)

def train_func(sub_train_, batch_size,optimizer, model, criterion, scheduler, device):

    # Train the model
    train_loss = 0
    train_acc = 0
    data = DataLoader(sub_train_, batch_size=batch_size, shuffle=True,
                      collate_fn=generate_batch)
    for i, (text, offsets, cls) in enumerate(data):
        optimizer.zero_grad()
        text, offsets, cls = text.to(device), offsets.to(device), cls.to(device)
        output = model(text, offsets)
        loss = criterion(output, cls)
        train_loss += loss.item()
        loss.backward()
        optimizer.step()
        train_acc += (output.argmax(1) == cls).sum().item()

    # Adjust the learning rate
    scheduler.step()

    return train_loss / len(sub_train_), train_acc / len(sub_train_)

def test(data_, batch_size, model, criterion, device):
    loss = 0
    acc = 0
    data = DataLoader(data_, batch_size=batch_size, collate_fn=generate_batch)
    for text, offsets, cls in data:
        text, offsets, cls = text.to(device), offsets.to(device), cls.to(device)
        with torch.no_grad():
            output = model(text, offsets)
            loss = criterion(output, cls)
            loss += loss.item()
            acc += (output.argmax(1) == cls).sum().item()

    return loss / len(data_), acc / len(data_)


def predict(text, model, vocab, ngrams):
    tokenizer = get_tokenizer("basic_english")
    with torch.no_grad():
        text = torch.tensor([vocab[token]
                            for token in ngrams_iterator(tokenizer(text), ngrams)])
        output = model(text, torch.tensor([0]))
        return output.argmax(1).item() + 1

def main(run, data_path, output_path, log_path, layer_width, batch_size, epochs, learning_rate, device):
    info('Data')
    # Get data
    yelp_train_dataset, yelp_test_dataset = download(batch_size)
    VOCAB_SIZE = len(yelp_train_dataset.get_vocab())
    EMBED_DIM = 32
    #batch_size = 16
    NUN_CLASS = len(yelp_train_dataset.get_labels())
    model = TextSentiment(VOCAB_SIZE, EMBED_DIM, NUN_CLASS).to(device)

    N_EPOCHS = epochs
    #min_valid_loss = float('inf')

    #activation function
    criterion = torch.nn.CrossEntropyLoss().to(device)
    #Stochastic Gradient descient with optimizer
    optimizer = torch.optim.SGD(model.parameters(), lr=learning_rate)

    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, 1, gamma=0.9)

    train_len = int(len(yelp_train_dataset) * 0.95)
    train_split_data, valid_split_data = random_split(yelp_train_dataset, [train_len, len(yelp_train_dataset) - train_len])

    info('Training')


    for epoch in range(N_EPOCHS):

        start_time = time.time()
        train_loss, train_acc = train_func(train_split_data, batch_size, optimizer, model, criterion, scheduler, device)
        valid_loss, valid_acc = test(valid_split_data, batch_size, model, criterion, device)

        secs = int(time.time() - start_time)
        mins = secs / 60
        secs = secs % 60

        print('Epoch: %d' %(epoch + 1), " | time in %d minutes, %d seconds" %(mins, secs))
        print(f'\tLoss: {train_loss:.4f}(train)\t|\tAcc: {train_acc * 100:.1f}%(train)')
        print(f'\tLoss: {valid_loss:.4f}(valid)\t|\tAcc: {valid_acc * 100:.1f}%(valid)')

    file_output = os.path.join(output_path, 'latest.hdf5')
    print('Serializing h5 model to:\n{}'.format(file_output))
    model.save(file_output)

    info('Test')

    test_loss, test_acc = test(yelp_test_dataset,batch_size, model, criterion, device)
    print('\nTest loss:', test_loss)
    print('\nTest accuracy:', test_acc)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='nlp news')
    parser.add_argument('-d', '--data', help='directory to training and test data', default='.data')
    parser.add_argument('-g', '--logs', help='log directory', default='logs')
    parser.add_argument('-o', '--outputs', help='output directory', default='outputs')
    parser.add_argument('-e', '--epochs', help='number of epochs', default=5, type=int)
    parser.add_argument('-l', '--layer', help='number nodes in internal layer', default=128, type=int)
    parser.add_argument('-b', '--batch', help='batch size', default=32, type=int)
    parser.add_argument('-r', '--lr', help='learning rate', default=0.001, type=float)
    args = parser.parse_args()

    run = Run.get_context()
    offline = run.id.startswith('OfflineRun')
    print('AML Context: {}'.format(run.id))
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(device)

    args = {
        'run': run,
        'data_path': check_dir(args.data).resolve(),
        'output_path': check_dir(args.outputs).resolve(),
        'log_path': check_dir(args.logs).resolve(),
        'epochs': args.epochs,
        'layer_width': args.layer,
        'batch_size': args.batch,
        'learning_rate': args.lr,
        'device': device
    }

    # log output
    if not offline:
        for item in args:
            if item != 'run':
                run.log(item, args[item])

    info('Args')

    for i in args:
        print('{} => {}'.format(i, args[i]))

    main(**args)



# Resources:
# This example is from the [PyTorch Beginner Tutorial](https://pytorch.org/tutorials/beginner/text_sentiment_ngrams_tutorial.html)
