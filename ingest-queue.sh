#!/bin/sh
# An example batch: the sources in your corpus worth spending credit on, in
# the order worth spending it. This file is a template -- edit the paths below
# to name your own documents. Nothing here is required to use superday.
#
#   ./ingest-queue.sh --dry      list what would run, spend nothing
#   ./ingest-queue.sh --trial    3 chunks each, to see the yield before committing
#   ./ingest-queue.sh            the real thing
#
# Every ingest path below calls Gemini and costs quota, so two habits are worth
# keeping. Check a PDF has a real text layer before queueing it -- a scanned
# image burns calls and yields nothing. And run --trial first: three chunks
# tells you whether a document is question-shaped for a fraction of the price.
#
# Each `ingest-pdf` lands questions at status=needs_review. Follow the whole
# queue with:  enrich  ->  audit  ->  review  ->  autotag --all  ->  dupes
set -e

# Where your source documents live. `superday settings corpus_dir` sets the
# same directory for the ingest commands themselves.
C="${IB_CORPUS_DIR:-$HOME/Desktop/IB_Resources}"
MODE="$1"

run() {                       # run <pages> <label> <path>
  echo ""
  echo "=== $2  (${1}pp) ==="
  case "$MODE" in
    --dry)   echo "    would run: superday ingest-pdf \"$3\"" ;;
    --trial) ./superday ingest-pdf "$3" --max-chunks 3 ;;
    *)       ./superday ingest-pdf "$3" ;;
  esac
}

# Highest value first: dense, question-shaped, and not already covered. Replace
# these with your own -- the page counts are only there to set expectations
# about how long each one takes.
run 284 "Interview handbook"       "$C/HandBooks/handbook.pdf"
run 466 "Valuation manual"         "$C/HandBooks/valuation-manual.pdf"
run  83 "M&A and divestitures"     "$C/M_and_A_guide.pdf"
run  34 "VC term sheet guide"      "$C/VC/term-sheet-guide.pdf"

echo ""
echo "queue finished. Next: enrich, audit, review, autotag --all, dupes"
