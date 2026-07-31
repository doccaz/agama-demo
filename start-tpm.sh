#!/bin/bash

mkdir /tmp/mytpm
swtpm socket --tpmstate dir=/tmp/mytpm \
  --ctrl type=unixio,path=/tmp/mytpm/swtpm-sock \
  --tpm2 \
  --log level=20

