"""The kernel every other layer stands on (layer 0).

``errors`` is the refusal contract, ``evidence`` what a number rests on,
``results`` the value-plus-evidence shape every public call returns,
``money`` the currency a value is denominated in, and ``diagnostics`` how to
read an evidence chain. Nothing here knows what a rate, a curve or an
instrument is, and nothing here imports from any other layer.
"""
