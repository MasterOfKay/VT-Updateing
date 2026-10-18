from datetime import datetime, timedelta
import os
import logging
import random


log = logging.getLogger("NF-ESN")

def chrome_esn_generator(prefix="NFCDCH-01"):

    ESN_GEN = "".join(random.choice("0123456789ABCDEF") for _ in range(30))
    # One cache file per family. Sharing a single .esn meant switching DRM system
    # kept handing back the other family's ESN until the 6 hour timer expired.
    esn_file = f'.esn-{prefix}'

    def gen_file():
        with open(esn_file, 'w') as file:
            file.write(f'{prefix}-{ESN_GEN}')

    if not os.path.isfile(esn_file):
        log.warning(f"Generating a new {prefix} ESN")
        gen_file()

    file_datetime = datetime.fromtimestamp(os.path.getmtime(esn_file))
    time_diff = datetime.now() - file_datetime
    if time_diff > timedelta(hours=6):
        log.warning(f"Old ESN detected, Generating a new {prefix} ESN")
        gen_file()

    with open(esn_file, 'r') as f:
        esn =  f.read()

    return esn
