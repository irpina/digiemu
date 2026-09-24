"""Print the version a Windows build is made as, and check a release tag.

    python tools/ci/release_version.py [--tag vX.Y.Z]

The version is emu/portable.py's APP_VERSION: the app shows it in the
launcher's title and stamps it into every firmware folder it sets up, so a
zip built as anything else would call itself one version and be named
another. With --tag (the release workflow passes the pushed tag), the tag
must be exactly 'v' + APP_VERSION; bump APP_VERSION in a pull request first,
then tag the merge. Exit 0 prints the version; exit 1 says what is wrong.
"""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VERSION = re.compile(r'\d{1,5}\.\d{1,5}\.\d{1,5}')


def app_version(root=ROOT):
    """-> APP_VERSION, read from emu/portable.py's source (not imported)."""
    path = os.path.join(root, 'emu', 'portable.py')
    with open(path, encoding='utf-8') as fh:
        m = re.search(r"^APP_VERSION = '([^']*)'\s*$", fh.read(), re.M)
    if m is None:
        raise ValueError('no APP_VERSION line in %s' % path)
    if not VERSION.fullmatch(m.group(1)):
        raise ValueError('APP_VERSION %r in %s is not x.y.z' % (m.group(1), path))
    return m.group(1)


def check_tag(tag, version):
    """-> None if `tag` releases `version`, else the reason it does not."""
    if not re.fullmatch(r'v' + VERSION.pattern, tag or ''):
        return 'tag %r is not a release tag: use vX.Y.Z' % (tag,)
    if tag[1:] != version:
        return ('tag %s does not match APP_VERSION %s in emu/portable.py: bump '
                'APP_VERSION to %s in a pull request, merge it, then tag that '
                'commit' % (tag, version, tag[1:]))
    return None


def main(argv=None, root=ROOT):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--tag', help='the release tag to check against APP_VERSION')
    args = ap.parse_args(argv)
    try:
        version = app_version(root)
    except (OSError, ValueError) as exc:
        print('release_version: %s' % exc, file=sys.stderr)
        return 1
    if args.tag is not None:
        problem = check_tag(args.tag, version)
        if problem:
            print('release_version: %s' % problem, file=sys.stderr)
            return 1
    print(version)
    return 0


if __name__ == '__main__':
    sys.exit(main())
