#! /bin/bash

CUDA_VISIBLE_DEVICES=1 python main.py -O --text "a DSLR photo of cthulhu" --workspace trial_cthulhu --iters 200
CUDA_VISIBLE_DEVICES=1 python main.py -O --text "a DSLR photo of a squirrel" --workspace trial_squirrel --iters 200
CUDA_VISIBLE_DEVICES=1 python main.py -O --text "a DSLR photo of a cat lying on its side batting at a ball of yarn" --workspace trial_cat_lying --iters 200