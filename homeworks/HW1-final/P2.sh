#!/bin/bash

# # 0. Word count
# cat apollo13.txt | wc

# A. Check how many lines have numbers
echo "A:"
grep -E "[0-9]" apollo13.txt | wc -l

# B. -c option in `grep`
echo "\nB:"
man grep | col -b | grep -A 1 "^ *-c" | tr "\n" " "

# C. .py files
echo "\nC:"
files="$(ls)"
let n_python=0
for f in $files
do
    if [ ${f##*.} == "py" ]
    then
        let n_python++
    fi
done
echo $n_python

# D. Files without "others" read/write access
echo "\nD:"
let n_no_read_write=0
for f in $(find . -maxdepth 1 -type f ! -perm -o=rw); do
    let n_no_read_write++
done
echo $n_no_read_write
