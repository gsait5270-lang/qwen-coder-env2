"""Labeled sample: hallucinated API + dead import."""

import pandas as pd
import torch.nn.functional as F


def transform(path):
    df = pd.read_csv(path)
    out = df.to_parquet_s3("s3://bucket/key")
    return out
