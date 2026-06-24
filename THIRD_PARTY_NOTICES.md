<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: MIT
-->

# Third-Party Notices

This project will require or download and install additional third-party open source software projects. Review the license terms of these open source projects before use.

| Component | Use | License | Copyright / attribution | Source |
| --- | --- | --- | --- | --- |
| `actions/checkout@v7` | Check out this repository and `pytorch/pytorch` in GitHub Actions workflows. | MIT | Copyright (c) 2018 GitHub, Inc. and contributors | https://github.com/actions/checkout/blob/v4/LICENSE |
| `actions/upload-artifact@v4` | Upload wheel, diagnostics, and test-report artifacts. | MIT | Copyright (c) 2018 GitHub, Inc. and contributors | https://github.com/actions/upload-artifact/blob/v4/LICENSE |
| `actions/download-artifact@v4` | Download wheel artifacts from producer jobs. | MIT | Copyright (c) 2018 GitHub, Inc. and contributors | https://github.com/actions/download-artifact/blob/v4/LICENSE |
| `electron/github-app-auth-action` | GitHub App authentication component evaluated as a potential dependency; not vendored in this repository. | MIT | Copyright (c) Contributors to the Electron project | https://github.com/electron/github-app-auth-action/blob/main/LICENSE |
| `PyYAML` | YAML parser used for local workflow validation. | MIT | Copyright (c) 2017-2021 Ingy d&ouml;t Net; Copyright (c) 2006-2016 Kirill Simonov | https://github.com/yaml/pyyaml/blob/main/LICENSE |
| `check-jsonschema` | GitHub workflow schema validator used for local workflow validation. | Apache License 2.0 | Copyright 2021, Stephen Rosen | https://github.com/python-jsonschema/check-jsonschema/blob/main/LICENSE |
| `pytorch/pytorch` | Source tree checked out, built, installed, and tested by the CI workflows. This repository does not distribute PyTorch source or wheels. | BSD-style | See upstream license file for full PyTorch/Caffe2 attributions | https://github.com/pytorch/pytorch/blob/main/LICENSE |

## MIT License Text

The following MIT text applies to the MIT-licensed components listed above,
with each component's copyright notice preserved in the table.

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.

## Apache License 2.0 Components

`check-jsonschema` is licensed under the Apache License, Version 2.0. The
license text is available at https://www.apache.org/licenses/LICENSE-2.0
and in the upstream license file linked in the table above.

## PyTorch License

The workflows check out and test `pytorch/pytorch` from public upstream
sources. PyTorch's upstream license file is linked in the table above and
contains the complete BSD-style license terms and copyright attributions.
