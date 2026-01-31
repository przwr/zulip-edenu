#!/bin/bash
# PORTAL EDENU: Script to update RealmUserDefault allow_private_data_export

echo "=========================================="
echo "PORTAL EDENU: Updating RealmUserDefault"
echo "=========================================="
echo ""
echo "This script will update the existing realm's RealmUserDefault"
echo "to set allow_private_data_export=True for new users."
echo ""

# Check if we're in the zulip-edenu directory
if [ ! -f "manage.py" ]; then
    echo "Error: Please run this script from the zulip-edenu directory"
    echo "Usage: cd zulip-edenu && ./scripts/update_default_data_export.sh"
    exit 1
fi

# Activate the virtual environment
if [ -f ".venv/bin/activate" ]; then
    echo "Activating virtual environment..."
    source .venv/bin/activate
else
    echo "Error: Virtual environment not found at .venv/bin/activate"
    echo "Please set up the Zulip development environment first."
    exit 1
fi

# Run the management command
echo ""
echo "Running management command..."
echo ""
python manage.py set_default_data_export

echo ""
echo "=========================================="
echo "Done!"
echo "=========================================="
