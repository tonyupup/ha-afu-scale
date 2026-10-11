#!/bin/sh
# 把本目录的 config 同步到 alioth-4 并（可选）重启测试 Pod。
# 节点上没有 rsync，用 tar over ssh；hostPath 持久化 .storage，重启很快。
# 用法: ./dev.sh [restart]
set -e
cd "$(dirname "$0")"
NODE=tony@172.18.1.12
D=ha-afu-test/config
ssh $NODE "mkdir -p $D && sudo rm -rf $D/custom_components"
tar -C config --exclude=__pycache__ -cf - configuration.yaml custom_components | ssh $NODE "tar -C $D -xf -"
case "$1" in
  restart) kubectl -n ha-afu-test rollout restart deploy/ha-afu-test ;;
esac
