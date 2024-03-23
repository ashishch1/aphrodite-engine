#include "moe_ops.h"

#include <torch/extension.h>

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    m.def("topk_softmax", &topk_softmax, "Apply top-k softmax to the gating outputs.");
}