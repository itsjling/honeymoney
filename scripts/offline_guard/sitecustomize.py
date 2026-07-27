"""Install the default test network guard in child Python processes."""

import ssl

from scripts.offline_network_guard import install

del ssl
install()
