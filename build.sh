#!/bin/bash

set -u

# Find project file automatically and ensure exactly one exists.
shopt -s nullglob
project_files=( *.kicad_pro )
shopt -u nullglob

if [ "${#project_files[@]}" -eq 0 ]; then
    echo "Error: No .kicad_pro file found in the current directory." >&2
    exit 1
fi

if [ "${#project_files[@]}" -gt 1 ]; then
    echo "Error: Multiple .kicad_pro files found in this directory:" >&2
    printf '%s\n' "${project_files[@]}" >&2
    echo "Please ensure only one .kicad_pro file is present." >&2
    exit 1
fi

PROJECT_FILE="${project_files[0]}"
PROJECT_NAME=$(basename -s .kicad_pro "$PROJECT_FILE")
JOBSET_FILE="Build.kicad_jobset"
BUILD_DIR="build"
OUTPUT_POS_FILE="$BUILD_DIR/positions.csv"
STRICT_JOBSET="${STRICT_JOBSET:-0}"

if [ ! -f "$JOBSET_FILE" ]; then
    echo "Error: Jobset file not found: '$JOBSET_FILE'" >&2
    exit 1
fi

if [ "$PROJECT_NAME" = "Template" ]; then
    echo "Warning: The project name is still 'Template'."
    echo "It is recommended to rename project files before proceeding."
    echo
fi

mkdir -p "$BUILD_DIR"

echo "Running KiCad jobset for '$PROJECT_FILE'..."
jobset_output=$(kicad-cli jobset run -f "$JOBSET_FILE" "$PROJECT_FILE" 2>&1)
jobset_exit=$?
printf '%s\n\n' "$jobset_output"

if [ $jobset_exit -ne 0 ]; then
    echo "Warning: KiCad jobset reported failures (exit $jobset_exit)." >&2
    echo "Continuing because STRICT_JOBSET=$STRICT_JOBSET..." >&2
fi

# Convert the raw position export produced by the jobset.
RAW_POS_FILE=""
for candidate in \
    "$BUILD_DIR/positions_raw-all-pos" \
    "$BUILD_DIR/positions_raw" \
    "$BUILD_DIR/positions_raw-all-pos.csv" \
    "$BUILD_DIR/positions_raw.csv"
do
    if [ -f "$candidate" ]; then
        RAW_POS_FILE="$candidate"
        break
    fi
done

if [ -z "$RAW_POS_FILE" ]; then
    echo "Error: No jobset position file found under '$BUILD_DIR/'." >&2
    exit 1
fi

echo "Converting component placement file..."
awk '
BEGIN {
    FS=",";
    OFS=",";
    print "Designator,Mid X,Mid Y,Layer,Rotation";
}
NR > 1 {
    gsub(/"/, "", $1);
    side_val = $NF;
    rot_val = $(NF-1);
    pos_y = $(NF-2);
    pos_x = $(NF-3);
    gsub(/\r/, "", side_val);
    side_lc = tolower(side_val);
    layer = (side_lc == "top" || side_lc == "front") ? "T" : "B";
    print $1, pos_x, pos_y, layer, rot_val;
}
' "$RAW_POS_FILE" > "$OUTPUT_POS_FILE"

echo "Conversion complete."
echo "Raw positions: $RAW_POS_FILE"
echo "Converted positions: $OUTPUT_POS_FILE"

if [ $jobset_exit -ne 0 ] && [ "$STRICT_JOBSET" = "1" ]; then
    echo "STRICT_JOBSET=1 is set, returning jobset failure exit code." >&2
    exit $jobset_exit
fi

echo "Build completed."
