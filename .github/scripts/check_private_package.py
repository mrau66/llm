"""Verify GHCR privacy without displaying the token or changing visibility."""
import argparse
import json
import os
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser()
parser.add_argument("--allow-missing", action="store_true")
args = parser.parse_args()
owner = os.environ["GITHUB_REPOSITORY_OWNER"]
package_name = os.getenv("GH_PACKAGE", "personal-knowledge-private")
url = "https://api.github.com/users/" + quote(owner, safe="") + "/packages/container/" + quote(package_name, safe="")
request = Request(url, headers={
    "Authorization": "Bearer " + os.environ["GH_TOKEN"],
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})
try:
    with urlopen(request, timeout=30) as response:
        package = json.load(response)
except HTTPError as error:
    if error.code == 404 and args.allow_missing:
        print("No visible existing package; GHCR creates new packages as private.")
        raise SystemExit(0)
    raise SystemExit(f"Cannot verify package visibility (HTTP {error.code}); publication check stopped.")
visibility = package.get("visibility")
print("Package visibility reported by GitHub:", visibility or "not supplied")
if visibility != "private":
    raise SystemExit("Cannot confirm a private package. Resolve its visibility before publishing.")
print("Verified: container package is private.")
