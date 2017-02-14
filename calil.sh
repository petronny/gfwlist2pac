#!/bin/sh
cd `dirname $0`
git reset --hard
git pull

git submodule update --init
cd gfwlist
git pull origin master
~/.local/bin/genpac --proxy="SOCKS5 127.0.0.1:1080" --gfwlist-proxy="SOCKS5 127.0.0.1:1080" -o ../gfwlist.pac --gfwlist-local=gfwlist.txt
cd ..

git add .
git commit -m "[$(LANG=C date)]auto update"
git push origin master
