#!/bin/bash

files="$(ls)"

for file in $files
do
    if [ -x $file ]; then
        echo $file "is executable"
    fi
done
