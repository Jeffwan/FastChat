#!/bin/bash

set -ex

echo ${MODEL_PATH} 

if [ ! -d /data/${MODEL_PATH} ]; then
    echo "Model weights directory does not exist"
    mkdir -p /data/${MODEL_PATH}
    pushd /data/${MODEL_PATH}
    VER=1.0.0.12
    wget https://luban-source.byted.org/repository/scm/toutiao.tos.toscli_${VER}.tar.gz -O toscli_${VER}.tar.gz
    rm -rf output && mkdir output && tar zxvf toscli_${VER}.tar.gz -C output && rm toscli_${VER}.tar.gz && mv output ${VER}
    mv ${VER}/toscli ./ 


    for f in $(./toscli -bucket llm --accessKey DFT9PESWBR1V7HBKLALJ list ${MODEL_PATH} | awk '{print $1}' | sed -n '6,$p')
    do
        echo "Processing $f"
        ./toscli -timeout 1h -bucket llm --accessKey DFT9PESWBR1V7HBKLALJ get $f
    done
    popd
else
  echo "Model weights exists"
fi