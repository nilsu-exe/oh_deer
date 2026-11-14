"""Bounded TAP metadata export shared by live discovery and mirror refreshes."""
import io
import time

from astropy.io.votable import parse
import requests

from . import config


def query_table(adql, *, maxrec=100000, max_bytes=512 * 1024 * 1024):
    started = time.monotonic()
    data = io.BytesIO()
    with requests.get(
        config.IRSA_BASE + "/TAP/sync",
        params={"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "votable",
                "MAXREC": maxrec, "QUERY": adql},
        timeout=(10, config.TAP_HTTP_TIMEOUT), stream=True,
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_content(65536):
            if time.monotonic() - started > config.TAP_HTTP_TIMEOUT:
                raise requests.Timeout("TAP metadata export exceeded its time limit")
            if data.tell() + len(chunk) > max_bytes:
                raise ValueError("TAP metadata export exceeded its byte limit")
            data.write(chunk)
    data.seek(0)
    votable = parse(data)

    def check_infos(container):
        for info in container.infos:
            if info.name == "QUERY_STATUS" and str(info.value).upper() != "OK":
                raise ValueError(f"TAP returned {info.value}: {info.content or ''}")
        for resource in getattr(container, "resources", []):
            check_infos(resource)

    check_infos(votable)
    return votable.get_first_table().to_table(use_names_over_ids=True)
