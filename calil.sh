#!/bin/sh
cd `dirname $0`
git reset --hard
git pull

git submodule update --init
for i in gfwlist genpac
do
	(cd $i;git pull origin master)
done

virtualenv2 env
source env/bin/activate
(cd genpac;python2 setup.py install)

env/bin/genpac --proxy="SOCKS5 127.0.0.1:1080" --gfwlist-proxy="SOCKS5 127.0.0.1:1080" -o gfwlist.pac --gfwlist-local=gfwlist/gfwlist.txt
sed '3d' -i gfwlist.pac
deactivate

git add .
git commit -m "[$(LANG=C date)]auto update"
git push origin master
