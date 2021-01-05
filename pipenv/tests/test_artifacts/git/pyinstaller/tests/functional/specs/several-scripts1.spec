#-----------------------------------------------------------------------------
# Copyright (c) 2005-2018, PyInstaller Development Team.
#
# Distributed under the terms of the GNU General Public License with exception
# for distributing bootloader.
#
# The full license is in the file COPYING.txt, distributed with this software.
#-----------------------------------------------------------------------------

# Verify each script has it's own global vars (original case, see issue
# #2949).

app_name = "several-scripts1"

a = Analysis(['several-scripts/rt-hook-script.py',
              'several-scripts/main-script1.py'])
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(pyz,
          a.scripts,
          exclude_binaries=True,
          name=app_name,
          debug=False,
          console=True)
coll = COLLECT(exe,
               a.binaries,
               a.zipfiles,
               a.datas,
               name=app_name)
