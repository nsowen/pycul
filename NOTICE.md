# Notices and credits

pycul is licensed under the GNU General Public License, version 2 or (at your
option) any later version. See `LICENSE`.

pycul is an independent Python implementation. Its protocol knowledge is
derived from the following GPL-licensed projects. Where code structure,
constants or encodings follow these sources, the corresponding module says so
in its header.

| Project | Files used as reference | Copyright | License |
|---|---|---|---|
| [FHEM](https://fhem.de) | `FHEM/14_CUL_MAX.pm`, `FHEM/10_MAX.pm` | (c) 2012 Matthias Gehre, (c) 2019 Wzut | GPL-2.0-or-later |
| [FHEM](https://fhem.de) | `FHEM/00_CUL.pm` (RSSI conversion, command set) | (c) Rudolf Koenig and FHEM contributors | GPL-2.0-or-later |
| [culfw](http://culfw.de) / [a-culfw](https://github.com/heliflieger/a-culfw) | `clib/rf_moritz.c`, `clib/rf_moritz.h` | (c) Rudolf Koenig, Matthias Gehre and culfw contributors | GPL-2.0 |
| [cul2mqtt](https://github.com/hobbyquaker/cul2mqtt) | general design inspiration only | (c) Sebastian Raff | GPL-2.0 |

The eQ-3 MAX! radio protocol itself was reverse engineered by the FHEM
community; see also the [FHEM wiki](https://wiki.fhem.de/wiki/MAX).
