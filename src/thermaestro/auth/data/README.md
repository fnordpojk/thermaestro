# Password lists

Thermaestro refuses the passwords in these lists. Both are taken from SecLists
(`Passwords/Common-Credentials/`), lowercased, normalized to NFKC, sorted and without
duplicates, one per line.

- `common-passwords.txt`: the entries of 4 characters or more from `10k-most-common.txt`
  (sha256 of the source file
  `68782d6a4a19a4768d5f15dd66bd534e7a33055cc755411e33f16d18c50fdcce`). Refused as they
  are, and with digits or symbols around them.
- `breached-long-passwords.txt`: the entries of 15 characters or more from
  `xato-net-10-million-passwords-1000000.txt` (sha256 of the source file
  `424a3e03a17df0a2bc2b3ca749d81b04e79d59cb7aeec8876a5a3f308d0caf51`), the million most
  common passwords of Mark Burnett's 2015 data set of ten million, which he released to
  the public domain. Refused as they are.

SecLists is under the MIT License:

    MIT License

    Copyright (c) 2018 Daniel Miessler

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
