#!/bin/sh
cd `dirname $0`
git reset --hard
git pull

git submodule update --init
cd gfwlist
git pull origin master
cd ..

git add .
git commit -m "[$(LANG=C date)]auto update"
git push origin master
