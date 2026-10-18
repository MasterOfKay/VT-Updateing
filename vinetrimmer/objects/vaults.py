import os
import sqlite3
import requests
import json
import traceback
from enum import Enum
import logging
import pymysql

from vinetrimmer.utils.AtomicSQL import AtomicSQL
from vinetrimmer.utils.collections import first_or_none


class InsertResult(Enum):
    FAILURE = 0
    SUCCESS = 1
    ALREADY_EXISTS = 2


class Vault:
    """
    Key Vault.
    This defines various details about the vault, including its Connection object.
    """

    def __init__(
        self,
        type_,
        name,
        ticket=None,
        path=None,
        username=None,
        password=None,
        database=None,
        key=None,
        host=None,
        port=3306,
    ):
        from vinetrimmer.config import directories

        try:
            self.type = self.Types[type_.upper()]
        except KeyError:
            raise ValueError(f"Invalid vault type [{type_}]")
        self.name = name
        self.con = None
        if self.type == Vault.Types.LOCAL:
            if not path:
                raise ValueError("Local vault has no path specified")
            self.con = sqlite3.connect(
                os.path.expanduser(path).format(data_dir=directories.data)
            )
        elif self.type == Vault.Types.REMOTE:
            self.con = pymysql.connect(
                user=username,
                password=password or "",
                db=database,
                host=host,
                port=port,
                cursorclass=pymysql.cursors.DictCursor,  # TODO: Needed? Maybe use it on sqlite3 too?
            )
            self.database=database
        elif self.type == Vault.Types.HTTP:
            self.url = host
            self.headers = {"decrypt-labs-api-key": key}
            self.get_cached_keys_path = "/get-cached-keys"
            self.insert_cached_keys_path = "/insert-cached-keys"
            self.username = username
            self.password = password
        elif self.type == Vault.Types.HTTPAPI:
            self.url = host
            self.password = password
        else:
            raise ValueError(f"Invalid vault type [{self.type.name}]")
        self.ph = {
            self.Types.LOCAL: "?",
            self.Types.REMOTE: "%s",
            self.Types.HTTP: None,
            self.Types.HTTPAPI: None,
        }[self.type]
        self.ticket = ticket

        self.perms = self.get_permissions()
        if not self.has_permission("SELECT"):
            raise ValueError(
                f"Cannot use vault. Vault {self.name} has no SELECT permission."
            )

    def __str__(self):
        return f"{self.name} ({self.type.name})"

    def get_permissions(self):
        if self.type == self.Types.LOCAL:
            return [tuple([["*"], tuple(["*", "*"])])]
        elif self.type == self.Types.HTTP:
            return [tuple([["*"], tuple(["*", "*"])])]
        elif self.type == self.Types.HTTPAPI:
            return [tuple([["*"], tuple(["*", "*"])])]

        with self.con.cursor() as c:
            c.execute("SHOW GRANTS")
            grants = c.fetchall()
            grants = [next(iter(x.values())) for x in grants]
        grants = [tuple(x[6:].split(" TO ")[0].split(" ON ")) for x in list(grants)]
        grants = [
            (
                list(map(str.strip, perms.replace("ALL PRIVILEGES", "*").split(","))),
                location.replace("`", "").split("."),
            )
            for perms, location in grants
        ]

        return grants

    def has_permission(self, operation, database=None, table=None):
        grants = [x for x in self.perms if x[0] == ["*"] or operation.upper() in x[0]]
        if grants and database:
            grants = [x for x in grants if x[1][0] in (database, "*")]
        if grants and table:
            grants = [x for x in grants if x[1][1] in (table, "*")]
        return bool(grants)

    class Types(Enum):
        LOCAL = 1
        REMOTE = 2
        HTTP = 3
        HTTPAPI = 4


class Vaults:
    """
    Key Vaults.
    Keeps hold of Vault objects, with convenience functions for
    using multiple vaults in one actions, e.g. searching vaults
    for a key based on kid.
    This object uses AtomicSQL for accessing the vault connections
    instead of directly. This is to provide thread safety but isn't
    strictly necessary.
    """

    # A vault that went offline may still accept the connection and never answer,
    # so never wait on one indefinitely.
    TIMEOUT = 15

    def __init__(self, vaults, service):
        self.adb = AtomicSQL()
        self.api_session_id = None
        self.log = logging.getLogger("Vaults")
        self.warned = set()  # (vault name, action) pairs already reported as broken
        self.vaults = sorted(
            vaults, key=lambda v: 0 if v.type == Vault.Types.LOCAL else 1
        )
        self.service = service.lower()
        for vault in self.vaults:
            if vault.type in (Vault.Types.HTTP, Vault.Types.HTTPAPI):
                continue

            try:
                vault.ticket = self.adb.load(vault.con)
                self.create_table(vault, self.service, commit=True)
            except (Exception, SystemExit) as e:
                # Keep the vault in the list, it may work again later. Every key
                # request guards itself and skips vaults that are still broken.
                self.vault_failed(vault, "initialize", e)

    def vault_failed(self, vault, action, e):
        """
        Report a vault as unusable without interrupting the download.
        Only the first failure per vault and action is logged, so a vault that is
        offline for the whole run doesn't spam the log for every single key.
        """
        self.log.debug(traceback.format_exc())
        if isinstance(e, SystemExit):
            return
        if (vault.name, action) in self.warned:
            return
        self.warned.add((vault.name, action))
        # Some libraries (e.g. requests) put a wall of text in str(e), the full
        # detail is in the debug log above, so only keep it short here.
        reason = " ".join(str(e).split())
        if len(reason) > 120:
            reason = reason[:120] + "..."
        self.log.warning(
            f" - Couldn't {action} vault {vault.name} ({e.__class__.__name__}: {reason})"
        )

    def request(self, method, url, key, params=None):
        r = requests.post(
            url,
            timeout=self.TIMEOUT,
            json={
                "method": method,
                "params": {
                    **(params or {}),
                    "session_id": self.api_session_id,
                },
                "token": key,
            },
        )

        if not r.ok:
            raise ValueError(f"API returned HTTP Error {r.status_code}: {r.reason.title()}")

        try:
            res = r.json()
        except json.JSONDecodeError:
            raise ValueError(f"API returned an invalid response: {r.text}")

        if res.get("status_code") != 200:
            raise ValueError(f"API returned an error: {res['status_code']} - {res['message']}")

        if session_id := res["message"].get("session_id"):
            self.api_session_id = session_id

        return res["message"]

    def __iter__(self):
        return iter(self.vaults)

    def get(self, kid, title):
        for vault in self.vaults:
            try:
                key = self.get_from_vault(vault, kid, title)
            except (Exception, SystemExit) as e:
                # The vault is offline or otherwise broken. It's only a cache, so
                # report it and move on to the next vault instead of aborting.
                self.vault_failed(vault, "get keys from", e)
                continue
            if key:
                return key, vault
        return None, None

    def get_from_vault(self, vault, kid, title):
        """
        Get the key for `kid` from a single vault.
        Returns None if the vault simply doesn't have the key, and raises if the
        vault itself couldn't be used.
        """
        if vault.type == Vault.Types.HTTP:
            try:
                res = requests.post(
                    vault.url + vault.get_cached_keys_path,
                    headers=vault.headers,
                    timeout=self.TIMEOUT,
                    json={
                        "service": self.service,
                        "kid": kid
                    }
                ).json()
            except Exception:
                # No usable answer from the newer endpoint, fall back to the old one.
                self.log.debug(traceback.format_exc())
            else:
                return res.get("key") if res.get("message") == "success" else None

            keys = requests.get(
                vault.url,
                timeout=self.TIMEOUT,
                params={
                    "service": self.service,
                    "username": vault.username,
                    "password": vault.password,
                    "kid": kid,
                },
            ).json()

            return first_or_none(x["key"] for x in (keys.get("keys") or []))

        elif vault.type == Vault.Types.HTTPAPI:
            keys = self.request(
                "GetKey",
                vault.url,
                vault.password,
                {
                    "kid": kid,
                    "service": self.service,
                    "title": title,
                },
            ).get("keys", [])
            return first_or_none(x["key"] for x in keys if x["kid"] == kid)

        else:
            # Note on why it matches by KID instead of PSSH:
            # Matching cache by pssh is not efficient. The PSSH can be made differently by all different
            # clients for all different reasons, e.g. only having the init data, but the cached PSSH is
            # a manually crafted PSSH, which may not match other clients manually crafted PSSH, and such.
            # So it searches by KID instead for this reason, as the KID has no possibility of being different
            # client to client other than capitalization. There is an unknown with KID matching, It's unknown
            # for *sure* if the KIDs ever conflict or not with another bitrate/stream/title. I haven't seen
            # this happen ever and neither has anyone I have asked.
            if not self.table_exists(vault, self.service):
                return None  # this service has no service table, so no keys, just skip
            if not vault.ticket:
                raise ValueError(
                    f"Vault {vault.name} does not have a valid ticket available."
                )
            c = self.adb.safe_execute(
                vault.ticket,
                lambda db, cursor: cursor.execute(
                    "SELECT `id`, `key_`, `title` FROM `{1}` WHERE `kid`={0}".format(
                        vault.ph, self.service
                    ),
                    [kid],
                ),
            ).fetchone()
            if not c:
                return None
            if isinstance(c, dict):
                c = list(c.values())
            if not c[2] and vault.has_permission("UPDATE", table=self.service):
                self.adb.safe_execute(
                    vault.ticket,
                    lambda db, cursor: cursor.execute(
                        "UPDATE `{1}` SET `title`={0} WHERE `id`={0}".format(
                            vault.ph, self.service
                        ),
                        [title, c[0]],
                    ),
                )
                self.commit(vault)
            return c[1]

    def table_exists(self, vault, table):
        if vault.type in (Vault.Types.HTTP, Vault.Types.HTTPAPI):
            return True
        if not vault.ticket:
            raise ValueError(
                f"Vault {vault.name} does not have a valid ticket available."
            )
        if vault.type == Vault.Types.LOCAL:
            return (
                self.adb.safe_execute(
                    vault.ticket,
                    lambda db, cursor: cursor.execute(
                        f"SELECT count(name) FROM sqlite_master WHERE type='table' AND name={vault.ph}",
                        [table],
                    ),
                ).fetchone()[0]
                == 1
            )
        return (
            list(
                self.adb.safe_execute(
                    vault.ticket,
                    lambda db, cursor: cursor.execute(
                        f"SELECT count(TABLE_NAME) FROM information_schema.TABLES WHERE table_schema = %s AND TABLE_NAME={vault.ph}",
                        (vault.database, [table]),
                    ),
                )
                .fetchone()
                .values()
            )[0]
            == 1
        )

    def create_table(self, vault, table, commit=False):
        if self.table_exists(vault, table):
            return
        if not vault.ticket:
            raise ValueError(
                f"Vault {vault.name} does not have a valid ticket available."
            )
        if vault.has_permission("CREATE"):
            print(f"Creating `{table}` table in {vault} key vault...")
            self.adb.safe_execute(
                vault.ticket,
                lambda db, cursor: cursor.execute(
                    "CREATE TABLE IF NOT EXISTS {} (".format(table)
                    + (
                        """
                        "id"        INTEGER NOT NULL UNIQUE,
                        "kid"       TEXT NOT NULL COLLATE NOCASE,
                        "key_"      TEXT NOT NULL COLLATE NOCASE,
                        "title"     TEXT,
                        PRIMARY KEY("id" AUTOINCREMENT),
                        UNIQUE("kid", "key_")
                        """
                        if vault.type == Vault.Types.LOCAL
                        else """
                        id          INTEGER AUTO_INCREMENT PRIMARY KEY,
                        kid         VARCHAR(64) NOT NULL,
                        key_        VARCHAR(64) NOT NULL,
                        title       TEXT,
                        UNIQUE(kid, key_)
                        """
                    )
                    + ");"
                ),
            )
            if commit:
                self.commit(vault)

    def insert_key(self, vault, table, kid, key, title, commit=False):
        try:
            return self.insert_key_into_vault(vault, table, kid, key, title, commit)
        except (Exception, SystemExit) as e:
            # Caching is best-effort, a broken vault must not stop the download.
            self.vault_failed(vault, "cache keys to", e)
            return InsertResult.FAILURE

    def insert_key_into_vault(self, vault, table, kid, key, title, commit=False):
        if vault.type == Vault.Types.HTTP:
            try:
                res = requests.post(
                            vault.url + vault.insert_cached_keys_path,
                            headers=vault.headers,
                            timeout=self.TIMEOUT,
                            json={
                                "service": self.service,
                                "kid": kid,
                                "key": key,
                                "title": title
                            }
                        ).json()
            except Exception:
                # No usable answer from the newer endpoint, fall back to the old one.
                self.log.debug(traceback.format_exc())
            else:
                if res.get("message") != "success":
                    return InsertResult.FAILURE
                return InsertResult.SUCCESS if res.get("inserted") else InsertResult.ALREADY_EXISTS

            res = requests.get(
                        vault.url,
                        timeout=self.TIMEOUT,
                        params={
                            "service": self.service,
                            "username": vault.username,
                            "password": vault.password,
                            "kid": kid,
                            "key": key,
                            "title": title
                        },
                    ).json()
            if res.get("status_code") != 200:
                return InsertResult.FAILURE
            return InsertResult.SUCCESS if res.get("inserted") else InsertResult.ALREADY_EXISTS
        elif vault.type == Vault.Types.HTTPAPI:
            res = self.request(
                "InsertKey",
                vault.url,
                vault.password,
                {
                    "kid": kid,
                    "key": key,
                    "service": self.service,
                    "title": title,
                },
            ).get("inserted")
            if res:
                return InsertResult.SUCCESS
            return InsertResult.ALREADY_EXISTS
        if not self.table_exists(vault, table):
            return InsertResult.FAILURE
        if not vault.ticket:
            raise ValueError(
                f"Vault {vault.name} does not have a valid ticket available."
            )
        if not vault.has_permission("INSERT", table=table):
            raise ValueError(
                f"Cannot insert key into Vault. Vault {vault.name} has no INSERT permission."
            )
        if self.adb.safe_execute(
            vault.ticket,
            lambda db, cursor: cursor.execute(
                "SELECT `id` FROM `{1}` WHERE `kid`={0} AND `key_`={0}".format(
                    vault.ph, self.service
                ),
                [kid, key],
            ),
        ).fetchone():
            return InsertResult.ALREADY_EXISTS
        self.adb.safe_execute(
            vault.ticket,
            lambda db, cursor: cursor.execute(
                "INSERT INTO `{1}` (kid, key_, title) VALUES ({0}, {0}, {0})".format(
                    vault.ph, table
                ),
                (kid, key, title),
            ),
        )
        if commit:
            self.commit(vault)
        return InsertResult.SUCCESS

    def commit(self, vault):
        if vault.type in (Vault.Types.HTTP, Vault.Types.HTTPAPI):
            return
        try:
            self.adb.commit(vault.ticket)
        except (Exception, SystemExit) as e:
            self.vault_failed(vault, "cache keys to", e)
