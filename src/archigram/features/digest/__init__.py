"""Compact token-efficient StructuralGraphIR and InteractionTraceIR Digest Generator."""

from __future__ import annotations

from archigram.features.digest.generator import (
    CandidateLeafSink,
    CandidateWrapperNode,
    CentralityHub,
    CommunityCluster,
    CommunityCoupling,
    ConcurrentFanoutHotspot,
    ContainmentClusterSummary,
    GraphDigestGenerator,
    ParticipantCallSummary,
    RecursiveCallHotspot,
    RepeatingLoopHotspot,
    StructuralDigest,
    TraceDigest,
)

__all__ = [
    "CandidateLeafSink",
    "CandidateWrapperNode",
    "CentralityHub",
    "CommunityCluster",
    "CommunityCoupling",
    "ConcurrentFanoutHotspot",
    "ContainmentClusterSummary",
    "GraphDigestGenerator",
    "ParticipantCallSummary",
    "RecursiveCallHotspot",
    "RepeatingLoopHotspot",
    "StructuralDigest",
    "TraceDigest",
]
