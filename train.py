import os
import time
import argparse
from pathlib import Path
from azureml.core.run import Run
import torch
from torchtext.data.utils import ngrams_iterator, get_tokenizer, ngrams_iterator
from torch.utils.data import DataLoader
import torch.nn as nn
import torch.nn.functional as F
from torchtext.datasets import TextClassificationDataset
from torch.utils.data.dataset import random_split
import pickle
import pandas as pd
import sys
from os import listdir
from os.path import isfile, join

###################################################################
# Helpers                                                         #
###################################################################

def info(msg, char = "#", width = 75):
    print("")
    print(char * width)
    print(char + "   %0*s" % ((-1*width)+5, msg) + char)
    print(char * width)


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

def load_vocab(filename):
    with open(filename, 'rb') as f:
        vocab = pickle.load(f)
        return vocab

def array_to_tensor(df_item):
    return torch.tensor(df_item['tensor'])

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

def train_func(train_dataset, batch_size,optimizer, model, criterion, scheduler, device):

    # Train the model
    train_loss = 0
    train_acc = 0

    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, collate_fn=generate_batch)

    for i, (text, offsets, cls) in enumerate(train_loader):
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

    return train_loss / len(train_loader), train_acc / len(train_loader)

###################################################################
# Testing                                                         #
###################################################################

def test(test_dataset, batch_size, model, criterion, device):
    loss = 0
    acc = 0


    test_loader = DataLoader(
        test_dataset,
        batch_size=batch_size, shuffle=False, collate_fn=generate_batch)

    for text, offsets, cls in test_loader:
        text, offsets, cls = text.to(device), offsets.to(device), cls.to(device)
        with torch.no_grad():
            output = model(text, offsets)
            loss = criterion(output, cls)
            loss += loss.item()
            acc += (output.argmax(1) == cls).sum().item()

    return loss / len(test_loader), acc / len(test_loader)


def predict(text, model, vocab, ngrams):
    tokenizer = get_tokenizer("basic_english")
    with torch.no_grad():
        text = torch.tensor([vocab[token]
                            for token in ngrams_iterator(tokenizer(text), ngrams)])
        output = model(text, torch.tensor([0]))
        return output.argmax(1).item() + 1

###################################################################
# Main                                                            #
###################################################################


def main(run, input_path, device):
    info('Data')
    # Get data
    # dataset object from the run
    #run = Run.get_context()
    print(f'input_path: {input_path}')
    #get all files from directory
    input_path = Path(input_path).resolve()
    print(f'input_path: {input_path}')
    file_list = [f for f in listdir(input_path) if isfile(join(input_path, f))]

    print(f'file list length: {len(file_list)}')
    train_df = pd.DataFrame(columns=['label', 'tensor'])
    vocab = None

    for file in file_list:
        if file.__contains__('parquet'):
            print(file)
            df = pd.read_parquet(Path(os.path.join(input_path, file)).resolve())
            print(len(df))
            train_df = train_df.append(df)
            print(f'length of loaded train_df: {len(train_df)}')
        else:
            vocab = load_vocab(Path(os.path.join(input_path,'vocab.pickle')).resolve())

    print(train_df.head())
    #create tensor and remove header row
    train_df['tensor'] = train_df.apply(array_to_tensor, axis=1)
    train_data = list(train_df.values)
    train_labels = set(train_df['label'])
    print(train_labels)
    full_dataset = TextClassificationDataset(vocab, train_data, train_labels)

    train_len = int(len(full_dataset) * 0.80)
    yelp_train_dataset, yelp_test_dataset = random_split(full_dataset, [train_len, len(full_dataset) - train_len])


    VOCAB_SIZE = len(full_dataset.get_vocab())
    EMBED_DIM = 32
    batch_size = 16
    NUM_CLASS = len(train_labels)

    print(f'create model VOCAB_SIZE: {VOCAB_SIZE} NUM_CLASS: {NUM_CLASS}')
    model = TextSentiment(VOCAB_SIZE, EMBED_DIM, NUM_CLASS).to(device)

    N_EPOCHS = 15
    #min_valid_loss = float('inf')

    #activation function
    criterion = torch.nn.CrossEntropyLoss().to(device)
    #Stochastic Gradient discent with optimizer
    optimizer = torch.optim.SGD(model.parameters(), lr=4.0)

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

    #file_output = os.path.join(output_path, 'latest.hdf5')
    #print('Serializing h5 model to:\n{}'.format(file_output))
    #model.save(file_output)

    info('Test')

    test_loss, test_acc = test(yelp_test_dataset,batch_size, model, criterion, device)
    print('\nTest loss:', test_loss)
    print('\nTest accuracy:', test_acc)

if __name__ == "__main__":
    # parser = argparse.ArgumentParser(description='nlp news')
    # parser.add_argument('-d', '--data', help='data guid for input datasets')
    # parser.add_argument('-g', '--logs', help='log directory', default='logs')
    # parser.add_argument('-o', '--outputs', help='output directory', default='outputs')
    # parser.add_argument('-e', '--epochs', help='number of epochs', default=10, type=int)
    # parser.add_argument('-b', '--batch', help='batch size', default=16, type=int)
    # parser.add_argument('-r', '--lr', help='learning rate', default=4.0, type=float)
    # args = parser.parse_args()
    input_path = sys.argv[1]
    print(f'input_path: {input_path}')


    run = Run.get_context()
    #dataset_reviews = run.input_datasets['prepared_reviews_ds']
    #print(f'dataset_reviews: {dataset_reviews}')

    offline = run.id.startswith('OfflineRun')
    print('AML Context: {}'.format(run.id))
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(device)
    # args = {
    #     'run': run,
    #     'input_data': args.data,
    #     'output_path': args.outputs,
    #     'log_path': args.logs,
    #     'epochs': args.epochs,
    #     'batch_size': args.batch,
    #     'learning_rate': args.lr,
    #     'device': device,
    # }

    # log output
    # if not offline:
    #     for item in args:
    #         if item != 'run':
    #             run.log(item, args[item])

    # info('Args')

    # for i in args:
    #     print('{} => {}'.format(i, args[i]))

    main(run, input_path, device)



# Resources:
# This example is from the [PyTorch Beginner Tutorial](https://pytorch.org/tutorials/beginner/text_sentiment_ngrams_tutorial.html)

