# -*- coding: utf-8 -*-
"""CAP6640_Lexical_Lullabies_Final_Project.ipynb

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/1XZpn-lbhA2Sc83d05veNaEUK-XXXT_a8

---

# Lexical Lullabies: LLM Based MIDI Generation with Music Theory Constraints
Final Project for CAP6640 by Christian King

---
## Part 0: Libraries, Downloads, Prerequites
---

In this section, we're just installing, importing, and setting up all the libraries and things we'll be using for the model.
"""

from google.colab import drive
drive.mount('/content/drive')

!pip install torch miditok transformers accelerate datasets evaluate miditoolkit midi2audio rouge_score

import os
import json
import random
import math

import torch
from torch.utils.data import Dataset, DataLoader

from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments
from transformers import default_data_collator
from datasets import load_dataset, Dataset as HFDataset
from evaluate import load
from tqdm.auto import tqdm
from collections import Counter

import miditok
from miditok import REMI

from huggingface_hub import notebook_login
notebook_login()

"""---
## Part 1: Preprocessing
---

### Move Over MIDICaps Dataset:

The difference in training time of the dataset being on google drive vs being local is huge. The difference in upload time from drive to local vs my computer to local is also huge. So through a lot of trial and error I found the best way of handling this is to upload a compressed version of the dataset to google drive and then move that to local and extract it here. \\

So in my case I've stored the dataset at /content/drive/MyDrive/MidiCaps/midicaps.tar.gz
along with the train.json and I'm just copying these over and then extracting them. I believe this is the fastest way to handle this in colab.
"""

!rsync -av --progress /content/drive/MyDrive/MidiCaps/midicaps.tar.gz /content/
!rsync -av --progress /content/drive/MyDrive/MidiCaps/train.json /content/
!tar -xzvf /content/midicaps.tar.gz -C /content/

"""
### Check Dataset and Create Vocab
Some midi files in this dataset aren't valid as they are missing the bare minimum requirements like a single pitch or duration token. So here we build functions to sort those files out and also build out this REMI token vocab that we are going to use to extend the Llama vocab."""

# check for essential midi token types
def is_valid_midi_tokens(tokens):
    has_pitch = any(token.startswith("Pitch_") for token in tokens)
    has_duration = any(token.startswith("Duration_") for token in tokens)
    has_velocity = any(token.startswith("Velocity_") for token in tokens)

    if not has_pitch:
        return False, "missing pitch token"
    if not has_duration:
        return False, "missing duration token"
    if not has_velocity:
        return False, "missing velocity token"

    return True, "valid"


def clean_dataset_and_build_vocab(hf_dataset, base_path, midi_tokenizer, midi_max_length=256, sample_limit=20000):
    # build valid sample list and vocabulary set
    valid_samples = []
    vocab_set = set()
    n = min(len(hf_dataset), sample_limit)

    for i in range(n):
        sample = hf_dataset[i]
        midi_path = os.path.join(base_path, sample["location"])
        try:
            # encode midi file into token sequence
            tok_seq = midi_tokenizer.encode(midi_path)
            tokens = tok_seq[0].tokens[:midi_max_length]
            valid, reason = is_valid_midi_tokens(tokens)
            if not tokens or not valid:
                print(f"skipped sample {i}: {reason}")
                continue
            midi_tokens = tokens.copy()

            # ensure start/end markers are present
            if "<MIDI_START>" not in midi_tokens:
                midi_tokens.insert(0, "<MIDI_START>")
            if "<MIDI_END>" not in midi_tokens:
                midi_tokens.append("<MIDI_END>")
            sample["midi_tokens"] = midi_tokens
            valid_samples.append(sample)
            vocab_set.update(midi_tokens)

        except Exception as e:
            # skip files that cause errors
            print(f"skipped sample {i} due to error: {e}")

    print(f"total valid samples: {len(valid_samples)} / {n}")

    return valid_samples, sorted(vocab_set)


def save_vocab(vocab_list, filepath):
    # write vocabulary to file, one token per line
    with open(filepath, "w") as f:
        for token in vocab_list:
            f.write(token + "\n")

    print(f"vocab ({len(vocab_list)} tokens) saved to {filepath}")


def save_valid_samples(valid_samples, filepath):
    # save valid samples as json
    with open(filepath, "w") as f:
        json.dump(valid_samples, f)

    print(f"saved {len(valid_samples)} samples to {filepath}")


def load_valid_samples(filepath):
    # load samples from json file
    with open(filepath, "r") as f:
        valid_samples = json.load(f)

    print(f"loaded {len(valid_samples)} samples from {filepath}")
    return valid_samples

"""### Create Dataset
Now that we have a our valid MIDI functions, we can actually create the dataset.
"""

class MidicapsMIDIDataset(Dataset):
    def __init__(self, samples, base_path, tokenizer, midi_tokenizer, max_length=512, midi_max_length=256):
        self.samples = samples  # list of data dicts
        self.base_path = base_path  # root path for midi files
        self.tokenizer = tokenizer  # text tokenizer
        self.midi_tokenizer = midi_tokenizer  # midi tokenizer
        self.max_length = max_length  # max tokens for text and midi
        self.midi_max_length = midi_max_length  # max midi tokens

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        sample = self.samples[idx]

        #remove spaces
        caption = sample["caption"].strip()
        midi_tokens = sample.get("midi_tokens", [])

        if not midi_tokens:
            # encode midi on the fly if not pre-tokenized
            midi_path = os.path.join(self.base_path, sample["location"])
            try:
                tok_seq = self.midi_tokenizer.encode(midi_path)
                midi_tokens = tok_seq[0].tokens[:self.midi_max_length]

                # add start/end markers if missing
                if "<MIDI_START>" not in midi_tokens:
                    midi_tokens.insert(0, "<MIDI_START>")
                if "<MIDI_END>" not in midi_tokens:
                    midi_tokens.append("<MIDI_END>")

            except Exception:
                # skip samples that error on midi
                print(f"skipped idx {idx} due to midi error")
                pad_id = self.tokenizer.pad_token_id

                return {
                    "input_ids": torch.tensor([pad_id] * self.max_length, dtype=torch.long),
                    "attention_mask": torch.tensor([0] * self.max_length, dtype=torch.long),
                    "labels": torch.tensor([-100] * self.max_length, dtype=torch.long),
                    "midi_mask": torch.tensor([0] * self.max_length, dtype=torch.long)
                }

        # merge caption and midi tokens for input
        midi_section = " ".join(midi_tokens)
        full_text = caption + " " + midi_section
        tokenized = self.tokenizer(
            full_text,
            truncation=True,
            max_length=self.max_length,
            add_special_tokens=False
        )
        input_ids = tokenized["input_ids"]
        attention_mask = tokenized["attention_mask"]

        # locate midi start marker in input_ids
        midi_start_id = self.tokenizer.convert_tokens_to_ids("<MIDI_START>")
        try:
            midi_start_idx = input_ids.index(midi_start_id)
        except ValueError:
            midi_start_idx = len(input_ids)

        # only predict midi tokens, ignore caption in loss
        labels = [-100] * midi_start_idx + input_ids[midi_start_idx:]
        labels = labels[:self.max_length]

        # pad sequences to fixed length
        pad_len = self.max_length - len(input_ids)
        input_ids += [self.tokenizer.pad_token_id] * pad_len
        attention_mask += [0] * pad_len
        labels += [-100] * pad_len

        # build midi_mask to flag midi positions
        midi_mask = [0] * self.max_length
        for i in range(midi_start_idx, self.max_length):
            midi_mask[i] = 1

        return {
            "input_ids": torch.tensor(input_ids, dtype=torch.long),
            "attention_mask": torch.tensor(attention_mask, dtype=torch.long),
            "labels": torch.tensor(labels, dtype=torch.long),
            "midi_mask": torch.tensor(midi_mask, dtype=torch.long)
        }

"""---
## Part 2: Training
---

### Expand Llama 3 model vocabulary to incorporate MIDI:
"""

def prepare_tokenizer(remi_vocab_file: str):
    tokenizer = AutoTokenizer.from_pretrained("meta-llama/Llama-3.2-1B")
    # use eos token as pad token
    tokenizer.pad_token = tokenizer.eos_token
    # midi start and end tokens
    special_tokens = ["<MIDI_START>", "<MIDI_END>"]

    # add remi vocab collected from data
    if remi_vocab_file is not None and os.path.exists(remi_vocab_file):
        with open(remi_vocab_file, "r") as f:
            remi_tokens = [line.strip() for line in f if line.strip()]
        special_tokens.extend(remi_tokens)

    # expand tokenizer
    tokenizer.add_special_tokens({"additional_special_tokens": special_tokens})

    return tokenizer

"""### Load Llama 3 model:"""

def prepare_model(tokenizer):
    model = AutoModelForCausalLM.from_pretrained(
        "meta-llama/Llama-3.2-1B",
        device_map="auto",
        torch_dtype=torch.bfloat16, # for speed
        use_cache=False # for space
    )
    # update tokenizer to our expanded one
    model.resize_token_embeddings(len(tokenizer))

    # dropout to encourage even token distribution
    model.config.hidden_dropout_prob = 0.15
    model.config.attention_probs_dropout_prob = 0.15

    return model

"""### MIDI Trainer
This custom trainer is primarily used to increase loss on the midi part of the training.
"""

class CustomMidiTrainer(Trainer):
    def __init__(self, midi_loss_weight=3.0, inv_weights=None, tokenizer=None, **kwargs):
        # loss multiplier for midi tokens
        self.midi_loss_weight = midi_loss_weight
        self.inv_weights = inv_weights
        self._tokenizer = tokenizer
        super().__init__(**kwargs)

    def compute_loss(self, model, inputs, return_outputs=False, **kwargs):
        # forward pass to get logits
        outputs = model(**inputs)
        logits = outputs.logits
        labels = inputs["labels"]  # padded label ids with -100 for ignore

        # compute token cross entropy loss without reduction
        loss_fct = torch.nn.CrossEntropyLoss(
            reduction="none", ignore_index=-100, label_smoothing=0.05
        )
        # flatten for loss then reshape back
        loss = loss_fct(
            logits.view(-1, logits.size(-1)), labels.view(-1)
        ).view(labels.size())

        midi_mask = inputs.get("midi_mask")

        # apply custom weighting if midi mask and inv_weights available
        if midi_mask is not None and self.inv_weights and self._tokenizer:
            vocab_size = len(self._tokenizer)
            device = labels.device

            # start with ones and set inverse frequency weights
            weights_vector = torch.ones(vocab_size, device=device)
            for token_id, weight in self.inv_weights.items():
                if token_id < vocab_size:
                    weights_vector[token_id] = weight

            # select weights per label
            token_weights = weights_vector[labels]

            # boost midi token loss by midi_loss_weight
            final_weights = torch.where(
                midi_mask == 1,
                self.midi_loss_weight * token_weights,
                torch.ones_like(token_weights)
            )
            loss = loss * final_weights

        # average over all tokens
        loss = loss.mean()
        return (loss, outputs) if return_outputs else loss

def compute_inverse_frequency_weights(samples, tokenizer, smooth: int = 1):
    # count occurrences of token ids
    counter = Counter()

    for s in samples:
        # tokenize caption without special tokens
        cap_ids = tokenizer(s["caption"], add_special_tokens=False).input_ids
        counter.update(cap_ids)

        midi = s.get("midi_tokens", [])
        if isinstance(midi, str):
            midi_list = midi.split()
        else:
            midi_list = midi

        # convert midi tokens to ids
        midi_ids = tokenizer.convert_tokens_to_ids(midi_list)
        counter.update(midi_ids)

    total = sum(counter.values())

    # compute inverse weights
    inv = {tok: total / (count + smooth) for tok, count in counter.items()}

    # normalize
    mean_w = sum(inv.values()) / len(inv)
    inv = {tok: w / mean_w for tok, w in inv.items()}
    return inv

"""### Finetune Llama 3 on the MIDICaps Data:

#### Tensorboard:
"""

# Commented out IPython magic to ensure Python compatibility.
# %load_ext tensorboard
# %tensorboard --logdir ./logs

"""#### Training Loop:"""

def train():
    base_path = "/content/"
    json_path = os.path.join(base_path, "train.json")
    cleaned_vocab_file = os.path.join(base_path, "remi_vocab_cleaned_100.txt")
    valid_samples_file = os.path.join(base_path, "valid_samples_100.json")
    checkpoint_dir = "./llama_midicaps_model_final7"

    # load dataset from json file
    dataset_dict = load_dataset("json", data_files=json_path)["train"]

    midi_tokenizer = REMI()

    # resume from save if available
    if os.path.exists(valid_samples_file) and os.path.exists(cleaned_vocab_file):
        valid_samples = load_valid_samples(valid_samples_file)
        with open(cleaned_vocab_file, "r") as f:
            remi_vocab = [line.strip() for line in f if line.strip()]
    else:
        # clean dataset and build remi vocab
        valid_samples, remi_vocab = clean_dataset_and_build_vocab(
            hf_dataset=dataset_dict,
            base_path=base_path,
            midi_tokenizer=midi_tokenizer,
            midi_max_length=256,
            sample_limit=100000
        )
        save_vocab(remi_vocab, cleaned_vocab_file)
        save_valid_samples(valid_samples, valid_samples_file)

    # split data
    cleaned_dataset = HFDataset.from_list(valid_samples)
    split_dataset = cleaned_dataset.train_test_split(test_size=0.1, seed=6640)
    train_dataset_hf = split_dataset["train"]

    tokenizer = prepare_tokenizer(cleaned_vocab_file)
    model = prepare_model(tokenizer)

    # compute token weights to emphasize rare tokens
    inv_weights = compute_inverse_frequency_weights(valid_samples, tokenizer)

    # wrap data into custom dataset class
    train_dataset = MidicapsMIDIDataset(
        train_dataset_hf,
        base_path,
        tokenizer,
        midi_tokenizer,
        max_length=512,
        midi_max_length=256
    )

    # setup training args
    training_args = TrainingArguments(
        output_dir=checkpoint_dir,
        num_train_epochs=3,
        per_device_train_batch_size=4,
        gradient_accumulation_steps=4,
        learning_rate=2e-5,
        weight_decay=0.01,
        lr_scheduler_type="cosine",
        warmup_ratio=0.2,
        logging_steps=50,
        save_steps=200,
        save_total_limit=2,
        bf16=True,
        report_to=["tensorboard"],
        logging_dir="./logs",
        max_grad_norm=1.0
    )

    # initialize custom trainer with midi loss
    trainer = CustomMidiTrainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        tokenizer=tokenizer,
        data_collator=default_data_collator,
        midi_loss_weight=2.0,
        inv_weights=inv_weights
    )

    trainer.train()

    # local save
    save_path = "./final_llama_midicaps_model"
    model.save_pretrained(save_path)
    tokenizer.save_pretrained(save_path)

    # drive backup save
    second_save_path = "/content/drive/MyDrive/Final-Model-LexLull"
    os.makedirs(second_save_path, exist_ok=True)
    model.save_pretrained(second_save_path)
    tokenizer.save_pretrained(second_save_path)


train()

"""## Part 3: Evaluation"""

model_dir = "/content/checkpoint-6500"
valid_json = "/content/valid_samples_100.json"
batch_size = 64

# load test data
with open(valid_json) as f:
    samples = json.load(f)
random.seed(6640) # in honor of cap6640

# few shot for context
few_shot = [
    "A gentle lullaby in C major. <MIDI_START> Program_0 Pitch_60 Duration_10 Velocity_40 <MIDI_END>",
    "Upbeat rock riff with power chords. <MIDI_START> Program_30 Pitch_50 Duration_5 Velocity_70 <MIDI_END>",
    "Funk bass line with syncopation. <MIDI_START> Program_40 Pitch_35 Duration_3 Velocity_80 <MIDI_END>"
]
context = " ".join(few_shot)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
tokenizer = AutoTokenizer.from_pretrained(model_dir, padding_side="left")
tokenizer.pad_token = tokenizer.eos_token
model = AutoModelForCausalLM.from_pretrained(model_dir).to(device)
model = model.half()
model.eval()

# generate predictions
prompts, generated_sequences, reference_sequences = [], [], []
for i in tqdm(range(0, len(samples), batch_size), desc="generating"):
    batch = samples[i : i + batch_size]
    batch_prompts = [f"{context} {s['caption'].strip()} <MIDI_START>" for s in batch]
    enc = tokenizer(batch_prompts, return_tensors="pt", padding=True, truncation=True).to(device)
    out = model.generate(
        enc.input_ids,
        attention_mask=enc.attention_mask,
        max_new_tokens=256,
        repetition_penalty=1.2,
        no_repeat_ngram_size=3,
        eos_token_id=tokenizer.convert_tokens_to_ids("<MIDI_END>"),
        pad_token_id=tokenizer.pad_token_id,
    )
    for j, seq in enumerate(out):
        gen_ids = seq.tolist()[enc.input_ids.shape[1]:]
        tokens = tokenizer.convert_ids_to_tokens(gen_ids)
        generated_sequences.append(" ".join(tokens))
        prompts.append(batch_prompts[j])
        reference_sequences.append(" ".join(batch[j].get("midi_tokens", [])))

# compute bleu and rouge scores
bleu_score = load("bleu").compute(predictions=generated_sequences, references=[[r] for r in reference_sequences])["bleu"] * 100
rouge_score = load("rouge").compute(predictions=generated_sequences, references=reference_sequences)["rougeL"] * 100

print(f"bleu-4: {bleu_score:.2f}")
print(f"rouge-l: {rouge_score:.2f}")

for k in range(5):
    print(f"prompt: {prompts[k]}")
    print(f"gen: {generated_sequences[k]}")