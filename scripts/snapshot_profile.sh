#!/usr/bin/env bash

# A snapshot contains generated data, so changing only terminals/time/rate does
# not invalidate it. Accept the immediately preceding formal profile when its
# data scale still matches (TPC-C 20, Twitter 100, unchanged YCSB).
snapshot_profile_matches() {
  local profile_file=${1:-artifacts/snapshots/workload_profile.sha256}
  [[ -s $profile_file ]] || return 1
  sha256sum --check --status "$profile_file" && return 0
  { grep -Fqx '78586521177741d40e17b21c0c9aa0678a5adcd8ba8573a907eaeb2b1b3cc71c  benchmarks/postgres/tpcc.xml' "$profile_file" ||
    grep -Fqx '0ffdcfa56ab3f71620021b32d20614bff5b2281f71bac49228801ce12c6fe699  benchmarks/postgres/tpcc.xml' "$profile_file"; } &&
    grep -Fqx 'adcc60cd73a8d05357a7dc338356129bc3bbe8b7915716981e79188d27f9824c  benchmarks/postgres/twitter.xml' "$profile_file" &&
    grep -Fqx 'c113e50abd8b219bc557b79c982f55e748c4e7defac8c05e04edc34bbd36a7cf  benchmarks/postgres/ycsb.xml' "$profile_file" &&
    { grep -q '<scalefactor>20</scalefactor>' benchmarks/postgres/tpcc.xml ||
      grep -q '<scalefactor>5</scalefactor>' benchmarks/postgres/tpcc.xml; } &&
    grep -q '<scalefactor>100</scalefactor>' benchmarks/postgres/twitter.xml
}

snapshot_workload_profile_matches() {
  local workload=$1
  local profile_file=${2:-artifacts/snapshots/workload_profile.sha256}
  local config="benchmarks/postgres/${workload}.xml"
  [[ -s $profile_file && -s $config ]] || return 1
  local expected current
  expected=$(awk -v file="$config" '$2 == file {print $1; exit}' "$profile_file")
  [[ -n $expected ]] || return 1
  current=$(sha256sum "$config" | awk '{print $1}')
  [[ $expected == "$current" ]] && return 0
  case "$workload" in
    tpcc)
      { [[ $expected == 78586521177741d40e17b21c0c9aa0678a5adcd8ba8573a907eaeb2b1b3cc71c ]] &&
          grep -q '<scalefactor>20</scalefactor>' "$config"; } ||
        { [[ $expected == 0ffdcfa56ab3f71620021b32d20614bff5b2281f71bac49228801ce12c6fe699 ]] &&
          grep -q '<scalefactor>5</scalefactor>' "$config"; }
      ;;
    twitter)
      [[ $expected == adcc60cd73a8d05357a7dc338356129bc3bbe8b7915716981e79188d27f9824c ]] &&
        grep -q '<scalefactor>100</scalefactor>' "$config"
      ;;
    ycsb)
      [[ $expected == c113e50abd8b219bc557b79c982f55e748c4e7defac8c05e04edc34bbd36a7cf ]]
      ;;
    *) return 1 ;;
  esac
}
