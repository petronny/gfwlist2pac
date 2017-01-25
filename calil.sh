#!/bin/sh
git submodule update --init
cd gfwlist
git pull origin master
cd ..
git add .
git commit -m "[$(LANG=C date)]auto update"
git push origin master
