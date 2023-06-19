##!/bin/bash

set -ex

BASEDIR=$(dirname "$0")

echo "try to download model..."
./${BASEDIR}/download_weights.sh

echo "starting model...";

HOST=$(ip addr show eth0 | grep inet6 | awk '{print $2}' | sed 's/\/..//' | sed -n '1p')

CONTROLLER_PSM=lab.llm.chatmodelcontroller
CONTROLLER_HOST=$(sd lookup ${CONTROLLER_PSM} | awk '{print $1}' | sed -n '6p')
CONTROLLER_PORT=$(sd lookup ${CONTROLLER_PSM} | awk '{print $2}' | sed -n '6p')
CONTROLLER_URL="http://[${CONTROLLER_HOST}]:${CONTROLLER_PORT}"

NUM_GPUS=${NUM_GPUS:-1}

python3 -m fastchat.serve.model_worker --model-path /data/${MODEL_PATH} --host :: --port ${PORT0} --worker-address "http://[${HOST}]:${PORT0}" --model-name ${MODEL_NAME} --controller-address ${CONTROLLER_URL} --limit-model-concurrency ${MAX_CONCURRENT_REQ} --num-gpus ${NUM_GPUS}