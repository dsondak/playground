#!/bin/bash

grep -E "[0-9]" apollo13.txt | wc -l

man grep | col -b | grep -A 1 "^ *-c" | tr "\n" " "

find . -regex ".*\.\py" | wc -l

find . -maxdepth 1 -type f ! -perm -o=rw | wc -l