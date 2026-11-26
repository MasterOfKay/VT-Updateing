"""
`python -m webui.vpn_test <country>`: one connection through the VPN, the same way a
download makes it (a separate process, so it reads the current vinetrimmer.yml).
Prints one line of JSON. Never prints the proxy address: it holds the login.
"""
import json
import logging
import sys

import requests

from webui import vpn


class _Stop(Exception):
    pass


def _stop(message):
    raise _Stop(str(message).strip(" -"))


def run(country):
    from vinetrimmer.services.BaseService import BaseService

    name = vpn.country_name(country)
    log = logging.getLogger("vpn-test")
    log.disabled = True
    log.exit = _stop
    service = BaseService.__new__(BaseService)
    service.session, service.force_proxy, service.log = requests.Session(), True, log
    try:
        proxy = service.get_proxy(country)
        if "://" not in proxy:
            proxy = f"https://{proxy}"
        seen = requests.get("https://ipwho.is/", proxies={"all": proxy}, timeout=40).json()
    except _Stop as e:
        return False, f"No proxy for {name}: {e}. Check the provider and its login."
    except requests.exceptions.ProxyError as e:
        if "407" in str(e):
            hint = ""
            if vpn.status()["provider"] == "nordvpn":
                hint = (" NordVPN needs its service credentials (Nord Account → NordVPN → Set up NordVPN manually), "
                        "not your email and password.")
            return False, "The provider rejected the login." + hint
        return False, f"The proxy for {name} can't be reached."
    except requests.RequestException as e:
        return False, f"Connecting through {name} failed ({type(e).__name__})."
    except Exception as e:
        return False, f"The test failed ({type(e).__name__})."
    came_out = str(seen.get("country_code") or "").lower()
    if came_out != country:
        return False, f"Connected, but websites see {vpn.country_name(came_out) or 'an unknown country'}, not {name}."
    return True, f"Connected. Websites see {name}."


if __name__ == "__main__":
    ok, message = run(sys.argv[1])
    print(json.dumps({"ok": ok, "message": message}))
