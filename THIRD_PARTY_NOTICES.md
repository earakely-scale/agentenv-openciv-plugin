# Third-party notices

This repository's own code is licensed under the Apache License 2.0 ([LICENSE](LICENSE)). It
references OpenCiv3 as a git submodule (`vendor/OpenCiv3`) and does not modify it. The env image
that the `Dockerfile` builds contains the third-party software below, each under its own licence.
The image carries this file, `LICENSE`, `NOTICE`, OpenCiv3's `LICENSE` and the .NET licence and
notices in `/opt/civbridge/licenses/`.

Nothing here grants permission to use any licensor's names or trademarks.

## In the bridge (`/opt/civbridge`)

| Component | Licence | Copyright | Source |
|---|---|---|---|
| OpenCiv3: `C7Engine.dll`, `QueryCiv3.dll`, the Lua ruleset in `Lua/` | MIT | OpenCiv3 contributors | <https://github.com/C7-Game/OpenCiv3>, at the commit the submodule pins. The engine is built from a copy with the patches in [`patches/`](patches) applied. |
| Blast: `Blast.dll` (part of the OpenCiv3 tree) | Apache-2.0, and blast.c's licence for the parts derived from it | 2012 James Telfer; blast.c 2003 Mark Adler | <https://github.com/jamestelfer/Blast>, `vendor/OpenCiv3/Blast` |
| Serilog 2.12.0, Serilog.Expressions 3.3.0, Serilog.Sinks.Console 4.0.1 | Apache-2.0 | Serilog Contributors | <https://github.com/serilog> |
| MoonSharp 2.0.0 (`MoonSharp.Interpreter.dll`) | BSD-3-Clause | 2014-2016 Marco Mastropaolo | <https://github.com/moonsharp-devs/moonsharp> |
| ini-parser-netstandard 2.5.2 (`INIFileParser.dll`) | MIT | 2008 Ricardo Amores Hernández | <https://github.com/rickyah/ini-parser> |
| .NET 8 runtime (the bridge is published self-contained) | MIT | .NET Foundation and Contributors | <https://github.com/dotnet/runtime>; its own third-party notices are in `/opt/civbridge/licenses/dotnet-ThirdPartyNotices.txt` |

The Apache-2.0 text is the one in [LICENSE](LICENSE). The other licence texts follow.

### OpenCiv3 (MIT)

```
MIT License

Copyright (c) OpenCiv3 contributors

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
```

### blast.c, from which Blast is translated

```
Copyright (C) 2003 Mark Adler
version 1.1, 16 Feb 2003

This software is provided 'as-is', without any express or implied
warranty.  In no event will the author be held liable for any damages
arising from the use of this software.

Permission is granted to anyone to use this software for any purpose,
including commercial applications, and to alter it and redistribute it
freely, subject to the following restrictions:

1. The origin of this software must not be misrepresented; you must not
    claim that you wrote the original software. If you use this software
    in a product, an acknowledgment in the product documentation would be
    appreciated but is not required.
2. Altered source versions must be plainly marked as such, and must not be
    misrepresented as being the original software.
3. This notice may not be removed or altered from any source distribution.

Mark Adler    madler@alumni.caltech.edu
```

### MoonSharp (BSD-3-Clause)

```
Copyright (c) 2014-2016, Marco Mastropaolo
All rights reserved.

Parts of the string library are based on the KopiLua project (https://github.com/NLua/KopiLua)
Copyright (c) 2012 LoDC

Visual Studio Code debugger code is based on code from Microsoft vscode-mono-debug project (https://github.com/Microsoft/vscode-mono-debug).
Copyright (c) Microsoft Corporation - released under MIT license.

Remote Debugger icons are from the Eclipse project (https://www.eclipse.org/).
Copyright of The Eclipse Foundation

The MoonSharp icon is (c) Isaac, 2014-2015

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

* Neither the name of the {organization} nor the names of its
  contributors may be used to endorse or promote products derived from
  this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### ini-parser (MIT)

```
The MIT License (MIT)

Copyright (c) 2008 Ricardo Amores Hernández

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

## In the client image only (`docker build --target client`)

The default image does not contain these. The `client` target adds them, for recordings with the real
client's view (docs/recording.md).

| Component | Licence | Copyright | Source |
|---|---|---|---|
| OpenCiv3 client (`C7`), built from the pinned submodule plus `client/FrameCapture.cs` | MIT | OpenCiv3 contributors | <https://github.com/C7-Game/OpenCiv3> |
| Godot Engine 4.4.1 .NET (editor binary and GodotSharp) | MIT | Godot Engine contributors | <https://github.com/godotengine/godot> |
| .NET 8 runtime (shared, in `/usr/share/dotnet`) | MIT | .NET Foundation and Contributors | <https://github.com/dotnet/runtime> |
| Xvfb, Mesa and the other Debian packages the target installs | their own licences, in `/usr/share/doc/*/copyright` | their authors | Debian |
| OpenCiv3's art, from `C7-Game/Assets` | **none**: mostly community art from CivFanatics, not licensed for redistribution | its artists | <https://github.com/C7-Game/Assets>, at the commit the client pins |

Because the client image contains the art, it is not pushed to a public registry. The image also holds the art
converted for the play page (`/opt/openciv3-client/webart`, made by `agentenv_openciv3.webart` when the image is
built), with the Noto Sans fonts the client's labels use (SIL Open Font License 1.1 and Apache-2.0, their licence
alongside); the env serves them only to the play page.

## In the Python environment

The env server and its dependencies are installed from PyPI: `agentenv-framework-protocol`
(Apache-2.0), `mcp` (MIT), `pydantic` (MIT), `pillow` (MIT-CMU), `imageio-ffmpeg` (BSD-2-Clause)
and what they depend on. Each package's licence files are in its `*.dist-info` directory under
`site-packages`.

`imageio-ffmpeg` bundles a static FFmpeg 7.0.2 build (from <https://johnvansickle.com/ffmpeg/>),
which the image links to `/usr/local/bin/ffmpeg` to encode recordings. That build is licensed under
the GNU General Public License, version 3 or later (`ffmpeg -L`); FFmpeg's source is at
<https://ffmpeg.org/>. The base image `python:3.12-slim` contains Debian packages, whose copyright
files are in `/usr/share/doc/*/copyright`.
