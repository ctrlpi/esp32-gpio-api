# Load the compiled application, or upgrader.mpy when POST /upgrade left upgrade.lock.

import os

try:
	os.stat("upgrade.lock")
	upgrading = True
except OSError:
	upgrading = False

if upgrading:
	import upgrader
else:
	import app
