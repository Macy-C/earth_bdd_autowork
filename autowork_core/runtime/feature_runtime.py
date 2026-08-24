"""构造并访问单个 Feature 生命周期内共享的页面和资源状态。

Builds and accesses page and resource state shared within one Feature.
"""

from dataclasses import dataclass, field

from autowork_core.common.variables import Stratify


@dataclass
class FeatureRuntimeState:
    pages: dict = field(default_factory=dict)
    locators: Stratify = field(default_factory=Stratify)
    data: Stratify = field(default_factory=Stratify)


def create_feature_state(run_state):
    return FeatureRuntimeState(
        locators=Stratify(),
        data=run_state.public_data.fork(),
    )