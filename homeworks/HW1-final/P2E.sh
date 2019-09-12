#!/bin/bash

# A script that counts and prints the number of lines in each file
# for f in $(find . -maxdepth 1 -type f); do
for f in $(ls); do
    echo $f $(wc -l < $f)
done