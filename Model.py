import torch
import torch.nn as nn
import time
import os
from torch.nn import functional as F
from torch.utils.data import Dataset
from torch.utils.data.dataloader import DataLoader
from dataclasses import dataclass

# hyperparameters
batch_size = 8 # how many independent sequences will we process in parallel?
block_size = 32
max_iters = 5000
eval_interval = 100
learning_rate = 3e-4
device = 'cuda' if torch.cuda.is_available() else 'cpu'
eval_iters = 200
n_embd = 128
n_head = 6
n_layer = 3
dropout = 0.2
# ------------


chars = [' ', ',', '_', 'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M',
        'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9']
class_map = {"0 - GENERAL" : 0, "1 - SITE INFORMATION" : 1, "2 - BUILDING PLANS" : 2, "3 - BUILDING ELEVATIONS" : 3, "4 - ENLARGED VIEWS": 4,
             "5 - WALL SECTIONS AND ELEVATIONS" : 5, "6 - PARTITION TYPES LEGENDS AND SCHEDULES" : 6, "7 - VERTICAL CIRCULATION" : 7,
             "8 - EXTERIOR DETAILS" : 8, "9 - INTERIOR DETAILS" : 9,  "D - DEMOLITION" : 10}

# data loading
def get_batch(split):
    # generate a small batch of data of inputs x and targets y
    data = train_data if split == 'train' else val_data
    data_out = train_outputs if split == 'train' else val_outputs
    ix = torch.randint(len(data), (batch_size,))
    x = torch.stack([data[i] for i in ix])
    y = torch.stack([data_out[i] for i in ix])
    x, y = x.to(device), y.to(device)
    return x, y

@torch.no_grad()
def estimate_loss():
    out = {}
    model.eval()
    for split in ['train', 'val']:
        losses = torch.zeros(eval_iters)
        for k in range(eval_iters):
            X, Y = get_batch(split)
            logits, loss = model(X, Y)
            losses[k] = loss.item()
        out[split] = losses.mean()
    model.train()
    return out

def get_Sample(input, printSample=False):
    input = input.strip().upper()
    lines = input.split(',')
    line = lines[0]
    sample = [0] * block_size
    size = [0]
    types = [0]
    for i in range(len(line)):
        try :
            sample[i] = chars.index(line[i])
        except :
            pass

    try :
        size[0] = (float)(lines[1])
    except: size[0] = 48
    
    try :
        types[0] = (float)(lines[2])
    except: types[0] = 0
    
    try :
        classification = lines[-1]
        classification = class_map[classification]
    except :
        classification = 0

    if printSample :
        print(input, sample, classification)
    return torch.tensor(sample), torch.tensor(size, dtype = torch.float), torch.tensor(types, dtype = torch.float), torch.tensor(classification)

class IndexDataset(Dataset):
    def __init__(self, lines):
        #self.txt_path = "/workspaces/OLF-Data/OLFNetworkData.txt"
        self.data = []
        self.chars = chars
        self.class_map = class_map
        self.max_len = block_size
        #with open('OLFNetworkData.txt', 'r', encoding='utf-8') as f:
            #text = f.read()
        for line in lines: # text.splitlines():
            name, view_size, view_type, sample = get_Sample(line)
            self.data.append([name, view_size, view_type, sample])
        self.stoi = {ch:i+1 for i,ch in enumerate(chars)}
    
    def __len__(self):
        return len(self.data)
    
    def __getitem__(self, idx):
        name, view_size, view_type, sample = self.data[idx]
        return name, view_size, view_type, sample
    
def create_datasets(input_file):
    with open(input_file, 'r') as f:
        data = f.read()
    inputs = data.splitlines()

    test_set_size = min(1000, int(len(inputs) * 0.1))
    rp = torch.randperm(len(inputs)).tolist()
    train_words = [inputs[i] for i in rp[:-test_set_size]]
    test_words = [inputs[i] for i in rp[-test_set_size:]]
    print(f"split up the dataset into {len(train_words)} training examples and {len(test_words)} test examples")

    train_dataset = IndexDataset(train_words)
    test_dataset = IndexDataset(test_words)
    return train_dataset, test_dataset

class InfiniteDataLoader:
    def __init__(self, dataset, **kwargs):
        train_sampler = torch.utils.data.RandomSampler(dataset, replacement=True, num_samples=int(1e10))
        self.train_loader = DataLoader(dataset, sampler=train_sampler, **kwargs)
        self.data_iter = iter(self.train_loader)
    
    def next(self):
        try:
            batch = next(self.data_iter)
        except StopIteration:
            self.data_iter = iter(self.train_loader)
            batch = next(self.data_iter)
        return batch

def evaluate(model, dataset, max_batches=None):
    model.eval()
    loader = DataLoader(dataset, shuffle=True, batch_size=batch_size, num_workers=0)
    losses = []
    for i, batch in enumerate(loader):
        batch = [t.to(device) for t in batch]
        A, B, C, D = batch
        logits, loss = model(A, B, C, D)
        losses.append(loss.item())
        if max_batches is not None and i >= max_batches:
            break
    mean_loss = torch.tensor(losses).mean().item()
    model.train()
    return mean_loss

class TimedHead(nn.Module):
    def __init__(self, head_size):
        super().__init__()
        self.key = nn.Linear(n_embd, head_size, bias = False)
        self.query = nn.Linear(n_embd, head_size, bias = False)
        self.value = nn.Linear(n_embd, head_size, bias = False)
        self.register_buffer('tril', torch.tril(torch.ones(block_size, block_size)))
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        B,T,C = x.shape
        k = self.key(x)
        q = self.query(x)

        wei = q @ k.transpose(-2, -1) * k.shape[-1]**-0.5
        wei = wei.masked_fill(self.tril[:T, :T] == 0, float('-inf'))
        wei = F.softmax(wei, dim=-1)
        wei = self.dropout(wei)
        v = self.value(x)
        out = wei @ v
        return out

class TimedMultiHeadAttention(nn.Module):
    def __init__(self, num_heads, head_size):
        super().__init__()
        self.heads = nn.ModuleList([TimedHead(head_size) for _ in range(num_heads)])
        self.proj = nn.Linear(head_size * num_heads, n_embd)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        out = torch.cat([h(x) for h in self.heads], dim=-1)
        out = self.dropout(self.proj(out))
        return out


class Head(nn.Module):
    def __init__(self, head_size):
        super().__init__()
        self.key = nn.Linear(n_embd, head_size, bias = False)
        self.query = nn.Linear(n_embd, head_size, bias = False)
        self.value = nn.Linear(n_embd, head_size, bias = False)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        B,C = x.shape
        k = self.key(x)
        q = self.query(x)

        wei = q @ k.transpose(-2, -1) * k.shape[-1]**-0.5
        wei = F.softmax(wei, dim=-1)
        wei = self.dropout(wei)
        v = self.value(x)
        out = wei @ v
        return out

class MultiHeadAttention(nn.Module):
    def __init__(self, num_heads, head_size):
        super().__init__()
        self.heads = nn.ModuleList([Head(head_size) for _ in range(num_heads)])
        self.proj = nn.Linear(head_size * num_heads, n_embd)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        out = torch.cat([h(x) for h in self.heads], dim=-1)
        out = self.dropout(self.proj(out))
        return out

class FeedForward(nn.Module):
    def __init__(self, n_embd):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(n_embd, 4 * n_embd),
            nn.ReLU(),
            nn.Linear(4 * n_embd, n_embd),
            nn.Dropout(dropout)
        )

    def forward(self, x):
        return self.net(x)

class Block(nn.Module):
    def __init__(self, n_embd, n_head):
        super().__init__()
        head_size = n_embd // n_head
        self.sa = MultiHeadAttention(n_head, head_size)
        self.ffwd = FeedForward(n_embd)
        self.ln1 = nn.LayerNorm(n_embd)
        self.ln2 = nn.LayerNorm(n_embd)

    def forward(self, x):
        x = x + self.sa(self.ln1(x))
        x = x + self.ffwd(self.ln2(x))
        return x

class TimedBlock(nn.Module):
    def __init__(self, n_embd, n_head):
        super().__init__()
        head_size = n_embd // n_head
        self.sa = TimedMultiHeadAttention(n_head, head_size)
        self.ffwd = FeedForward(n_embd)
        self.ln1 = nn.LayerNorm(n_embd)
        self.ln2 = nn.LayerNorm(n_embd)

    def forward(self, x):
        x = x + self.sa(self.ln1(x))
        x = x + self.ffwd(self.ln2(x))
        return x

class XfmrModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.token_embedding_table = nn.Embedding(len(chars), n_embd)
        self.position_embedding_table = nn.Embedding(block_size, n_embd)
        self.size_head = nn.Linear(1, n_embd, dtype = torch.float)
        self.type_head = nn.Linear(1, n_embd, dtype = torch.float)
        self.first_blocks = nn.Sequential(*[TimedBlock(n_embd, n_head=n_head) for _ in range(n_layer)])
        self.blocks = nn.Sequential(*[Block(n_embd, n_head = n_head) for _ in range(n_layer)])
        self.ln_f = nn.LayerNorm(n_embd)
        self.lm_head = nn.Linear(n_embd, len(class_map.items()))
        self.apply(self.__init__weights)

    def __init__weights(self, module):
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight, mean = 0.0, std = 0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
    
    def forward(self, A, B, C, targets = None):
        Batch, T = A.shape
        tok_emb = self.token_embedding_table(A)
        pos_emb = self.position_embedding_table(torch.arange(T, device = device))
        size_emb = self.size_head(B)
        type_emb = self.type_head(C)
        x = tok_emb + pos_emb
        x = self.first_blocks(x)
        x = torch.sum(x, dim=-2, keepdim = False)
        x = x + type_emb + size_emb
        x = self.blocks(x)
        x = self.ln_f(x)
        logits = self.lm_head(x)

        if targets is None:
            loss = None
        else:
            B, C = logits.shape
            logits = logits.view(B, C)
            loss_targets = torch.nn.functional.one_hot(targets, len(class_map.items()))
            loss_targets = loss_targets.view(B, len(class_map.items()))
            loss = F.cross_entropy(logits, loss_targets.type(torch.FloatTensor))

        return logits, loss

txt_path = "IndexNetworkData.txt"
path = "IndexNetwork.pt"
model = XfmrModel()
if os.path.isfile(path):
    statedict = torch.load(path)
    model.load_state_dict(statedict)

m = model.to(device)
print(sum(p.numel() for p in m.parameters())/1e6, 'M parameters')

optimizer = torch.optim.AdamW(model.parameters(), lr = learning_rate)

def RunTraining():
    train_dataset, test_dataset = create_datasets(txt_path)
    batch_loader = InfiniteDataLoader(train_dataset, batch_size = batch_size)

    best_loss = None
    step = 0

    while True:
        t0 = time.time()
        batch = batch_loader.next()
        batch = [t.to(device) for t in batch]
        A, B, C, D = batch

        logits, loss = model(A, B, C, D)

        model.zero_grad(set_to_none = True)
        loss.backward()
        optimizer.step()

        if device.startswith('cuda'):
            torch.cuda.synchronize()
        t1 = time.time()

        if step % 100 == 0:
            print(f"step {step} | loss {loss.item():.4f} | step time {(t1-t0)*1000:.2f}ms")

        if step > 0 and step % 500 == 0:
            train_loss = evaluate(model, train_dataset, max_batches=5 * batch_size)
            test_loss = evaluate(model, test_dataset, max_batches=5 * batch_size)
            print(f"step {step} train loss: {train_loss} test loss: {test_loss}")
            # save the model to disk if it has improved
            if best_loss is None or test_loss < best_loss:
                print(f"test loss {test_loss} is the best so far, saving model to {path}")
                torch.save(model.state_dict(), path)
                best_loss = test_loss
            
            
        #if step > 0 and step % 200 == 0:
        #    print_samples(num=10)

        step+=1

while True:
    usage = input("Train or Test?")
    if usage == "Test":
        test = ""
        while test != "X":
            text = input("Test your room name")
            sample = get_Sample(text, True)
            A, B, C, D = sample
            A = A.view(1, -1)
            B = B.view(1, -1)
            C = C.view(1, -1)
            print(A, B)
            logits, loss = model(A, B, C)
            print(logits)
            max = torch.argmax(logits)
            print(list(class_map.keys())[max])
    elif usage == "Train":
        RunTraining()