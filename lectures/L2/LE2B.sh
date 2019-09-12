#!/bin/bash

echo "Please enter a file: "
read newfile

# Add and show status
git status

# Ask if the user would like to continue
echo "Would you like to continue (Y/N)"
read answer

if [ $answer = "Y" ]; then
    echo "Enter your commit message"
    read msg
    # git commit -m $msg
elif [ $answer = "N" ]; then
    exit 1
else
    echo "Y or N only"
fi

# Display the results of git status
git status

# Ask the user if they would like to continue
echo "Would you like to continue? (Y/N)"
read continue

if [ $continue = "Y" ]; then
    git commit -m $msg
elif [ $continue = "N" ]; then
    exit %1
else
    echo "Y or N only"
fi
