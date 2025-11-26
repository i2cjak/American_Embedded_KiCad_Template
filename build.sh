#!/bin/bash

# A script to run the KiCad jobset and then process the output files.
# It first runs the kicad-cli command, checks for success, and if successful,
# it converts the component placement CSV data to a new format.

# --- Configuration ---
# Find the project file automatically. This script assumes there is only one .kicad_pro file.
PROJECT_FILE=$(ls *.kicad_pro 2>/dev/null)

# Check if a project file was found.
if [ -z "$PROJECT_FILE" ]; then
    echo "❌ Error: No .kicad_pro file found in the current directory." >&2
    exit 1
fi

# Check if multiple project files were found.
if [ $(echo "$PROJECT_FILE" | wc -w) -gt 1 ]; then
    echo "❌ Error: Multiple .kicad_pro files found in this directory:" >&2
    echo "$PROJECT_FILE" >&2
    echo "Please ensure only one .kicad_pro file is present." >&2
    exit 1
fi

# Extract project name to check if it's the default.
PROJECT_NAME=$(basename -s .kicad_pro "$PROJECT_FILE")

# Set other file and directory names
JOBSET_FILE="Build.kicad_jobset"
RAW_POS_FILE="build/positions_raw-all-pos"
OUTPUT_POS_FILE="build/positions.csv"
BUILD_DIR="build"

# --- Main Logic ---

# Check for default project name and issue a warning if found.
if [ "$PROJECT_NAME" = "Template" ]; then
    echo "⚠️  Warning: The project name is still 'Template'."
    echo "   It is recommended to rename the project files before proceeding."
    echo # Add a newline for readability.
fi

# Ensure the build directory exists where outputs will be placed.
mkdir -p "$BUILD_DIR"

# Announce the first step.
echo "🚀 Running KiCad jobset for '$PROJECT_FILE'..."

# Execute the KiCad command.
# We capture all output (stdout and stderr) into a variable for later inspection.
# 'tee /dev/tty' allows the output to be printed to the terminal in real-time.
output=$(kicad-cli jobset run -f "$JOBSET_FILE" "$PROJECT_FILE" 2>&1 | tee /dev/tty)

# Capture the exit code of the last command.
exit_code=$?

echo # Add a newline for better readability.

# Check if the command succeeded.
# A successful run must have an exit code of 0 AND contain "0 job failed" in its output.
if [ $exit_code -eq 0 ] && echo "$output" | grep -q "0 job failed"; then
    echo "✅ KiCad jobset completed successfully."

    # --- CSV Conversion Step ---
    echo "📄 Converting component placement file..."

    # Check if the expected input file was created by the jobset.
    if [ ! -f "$RAW_POS_FILE" ]; then
        echo "❌ Error: Input file for conversion not found: '$RAW_POS_FILE'" >&2
        echo "The KiCad jobset may not have generated the expected position file." >&2
        exit 1
    fi

    # Use awk to efficiently process the CSV and create the new format.
    awk '
    # This BEGIN block runs once before processing any lines.
    BEGIN {
        FS=","; # Set the input field separator to a comma.
        OFS=", "; # Set the output field separator to a comma followed by a space.
        print "Designator, Mid X, Mid Y, Layer, Rotation"; # Print the new header.
    }

    # This block runs for every line *except* the header (NR > 1).
    NR > 1 {
        gsub(/"/, "", $1); # Remove quotes from the first field (e.g., "C1" -> C1).
        
        # FIX: Parse from the end of the line ($NF).
        # This handles cases where the middle fields (Value or Package) contain commas
        # (e.g., "40V, 2A"), which shifts the forward indices.
        # The structure is always: Ref, [Variable Fields...], PosX, PosY, Rot, Side
        
        side_val = $NF;       # Last field
        rot_val = $(NF-1);    # 2nd to last
        pos_y = $(NF-2);      # 3rd to last
        pos_x = $(NF-3);      # 4th to last

        # Clean carriage returns just in case (Windows/Linux compatibility)
        gsub(/\r/, "", side_val);

        # Convert the "Side" column to a single letter.
        layer = (side_val == "top") ? "T" : "B";
        
        # Print the columns in the desired new order using the relative variables.
        print $1, pos_x, pos_y, layer, rot_val;
    }
    ' "$RAW_POS_FILE" > "$OUTPUT_POS_FILE"

    echo "✅ Conversion complete. Output saved to '$OUTPUT_POS_FILE'."
    exit 0 # Exit with a success code.

else
    # If the command failed, print an error message and exit.
    echo "❌ Error: KiCad jobset failed." >&2
    echo "Please review the output above for details." >&2
    exit 1 # Exit with a non-zero status to indicate failure.
fi
