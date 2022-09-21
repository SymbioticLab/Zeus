---
hide:
  - toc
  - navigation
  - footer
---
<div align="center">
<img src="assets/img/logo_dark.svg#only-dark" width="60%" alt="Zeus logo" style="margin-bottom: 1em">
<img src="assets/img/logo_light.svg#only-light" width="60%" alt="Zeus logo" style="margin-bottom: 1em">
<h1>An Energy Optimization Framework for DNN Training</h1>
</div>

Zeus automatically optimizes the **energy and time** of recurring DNN training jobs by finding the optimal **batch size** and **GPU power limit**.

Please refer to our [NSDI '23 publication](https://arxiv.org/abs/2208.06102){.external} for details.
Check out [Overview](overview/index.md) for a summary.

Zeus is part of [The ML.ENERGY Initiative](https://ml.energy){.external}.

## Getting Started

Refer to [Getting Started](getting_started/index.md) for instructions on environment setup, installation, and integration.
We also provide integration examples:

- [Integrating Zeus with computer vision](https://github.com/SymbioticLab/Zeus/tree/master/examples/cifar100){.external}
- [Integrating Zeus with NLP](https://github.com/SymbioticLab/Zeus/tree/master/examples/capriccio){.external}
- [Running trace-driven simulation on single recurring jobs and the Alibaba GPU cluster trace](https://github.com/SymbioticLab/Zeus/tree/master/examples/trace_driven){.external}
TODO: add

## Extending Zeus

You can easily implement custom policies for batch size and power limit optimization and plug it into Zeus.

Refer to [Extending Zeus](extend.md) for details.
