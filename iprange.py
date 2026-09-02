"""Turn "where to look for printers" into a list of addresses.

One place per line in config.ini's [ranges]:

    Office     = 10.0.10.0/24              a whole subnet
    Warehouse  = 10.0.20.50-10.0.20.99     a span
    Spares     = 10.0.20.50-99             the same span, short form
    Boardroom  = 10.0.30.15                just the one

A subnet gives you its usable hosts - the network and broadcast addresses are
not printers and are never probed. Anything that is not one of those four
shapes raises ValueError with a sentence a person can act on, because a typo
here would otherwise be a silent no-op: a range that finds nothing looks
exactly like a range with nothing in it.

This module is the authority on what an address list means. The console's
Print fleet tab checks the same shapes as you type, for immediate feedback,
but what actually gets scanned is decided here.
"""

from __future__ import annotations

import ipaddress

# A whole /22 is 1022 addresses - already a big scan. Bigger than this is
# almost always a typo (10.0.0.0/8 is 16 million), so it is refused rather
# than attempted.
DEFAULT_MAX_ADDRESSES = 1024


def _ip(text, whole):
    try:
        return ipaddress.IPv4Address(text)
    except ipaddress.AddressValueError:
        raise ValueError("'%s' is not an IPv4 address (in '%s')." % (text, whole))


def parse(spec, max_addresses=DEFAULT_MAX_ADDRESSES):
    """One place to look -> the list of addresses to probe, in order.

    Raises ValueError with a plain sentence for anything unusable.
    """
    text = str(spec or "").strip()
    if not text:
        raise ValueError("no address or range given.")
    if " " in text or "," in text:
        raise ValueError("'%s' should be one address or range - put each place on its own line." % text)
    if ":" in text:
        raise ValueError("'%s' has a port on it. Ranges are addresses only; a printer on a "
                         "nonstandard port goes in [devices]." % text)

    if "/" in text:
        try:
            net = ipaddress.IPv4Network(text, strict=False)
        except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError):
            raise ValueError("'%s' is not a subnet - it should look like 10.0.10.0/24." % text)
        # /31 and /32 have no spare network/broadcast address to skip.
        hosts = [net.network_address] if net.prefixlen == 32 else list(
            net if net.prefixlen == 31 else net.hosts())
        addresses = [str(h) for h in hosts]
    elif "-" in text:
        left, _, right = text.partition("-")
        start = _ip(left.strip(), text)
        right = right.strip()
        if right.count(".") == 3:
            end = _ip(right, text)
        elif right.isdigit():
            # 10.0.20.50-99: the short form everyone writes on a whiteboard.
            last = int(right)
            if last > 255:
                raise ValueError("'%s' ends past .255 - check the last number." % text)
            end = _ip("%s.%d" % (str(start).rsplit(".", 1)[0], last), text)
        else:
            raise ValueError("'%s' is not a range - it should look like 10.0.20.50-10.0.20.99 "
                             "or 10.0.20.50-99." % text)
        if int(end) < int(start):
            raise ValueError("'%s' runs backwards - the second address is lower than the first." % text)
        addresses = [str(ipaddress.IPv4Address(n)) for n in range(int(start), int(end) + 1)]
    else:
        addresses = [str(_ip(text, text))]

    if not addresses:
        raise ValueError("'%s' contains no usable addresses." % text)
    if max_addresses and len(addresses) > max_addresses:
        raise ValueError("'%s' is %s addresses, and the limit is %s - scanning that many would take "
                         "a long time and light up your network monitoring. Use a smaller range."
                         % (text, format(len(addresses), ","), format(max_addresses, ",")))
    return addresses


def looks_like_a_place(spec):
    """Cheap shape check - the same one the console does as you type."""
    try:
        parse(spec, max_addresses=0)
        return True
    except ValueError:
        return False
