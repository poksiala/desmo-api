import enum


class JailState(enum.StrEnum):
    uninitialized = enum.auto()
    jail_provisioning = enum.auto()
    dns_provisioning = enum.auto()
    jail_setup = enum.auto()
    jail_ready = enum.auto()
    jail_removal = enum.auto()
    dns_deprovisioning = enum.auto()
    terminated = enum.auto()


class JailEvent(enum.StrEnum):
    initialize = enum.auto()
    jail_provisioned = enum.auto()
    jail_provisioning_failed = enum.auto()
    dns_provisioned = enum.auto()
    dns_provisioning_failed = enum.auto()
    jail_setup_done = enum.auto()
    jail_setup_failed = enum.auto()
    remove_jail = enum.auto()
    jail_removed = enum.auto()
    jail_removal_failed = enum.auto()
    dns_deprovisioned = enum.auto()
    dns_deprovisioning_failed = enum.auto()
