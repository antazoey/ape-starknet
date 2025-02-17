# SPDX-License-Identifier: MIT
# OpenZeppelin Contracts for Cairo v0.1.0 (upgrades/library.cairo)

%lang starknet

from starkware.starknet.common.syscalls import get_caller_address
from starkware.cairo.common.cairo_builtins import HashBuiltin
from starkware.cairo.common.bool import TRUE, FALSE

#
# Events
#

@event
func Upgraded(implementation: felt):
end

#
# Storage variables
#

@storage_var
func Proxy_implementation_address() -> (implementation_address: felt):
end

@storage_var
func Proxy_admin() -> (proxy_admin: felt):
end

@storage_var
func Proxy_initialized() -> (initialized: felt):
end

#
# Initializer
#

namespace Proxy:

    #
    # Initializer
    #

    func initializer{
            syscall_ptr: felt*,
            pedersen_ptr: HashBuiltin*,
            range_check_ptr
        }(proxy_admin: felt):
        let (initialized) = Proxy_initialized.read()
        with_attr error_message("Proxy: contract already initialized"):
            assert initialized = FALSE
        end

        Proxy_initialized.write(TRUE)
        Proxy_admin.write(proxy_admin)
        return ()
    end

    #
    # Upgrades
    #

    func _set_implementation{
            syscall_ptr: felt*,
            pedersen_ptr: HashBuiltin*,
            range_check_ptr
        }(new_implementation: felt):
        Proxy_implementation_address.write(new_implementation)
        Upgraded.emit(new_implementation)
        return ()
    end

    #
    # Setters
    #

    func _set_admin{
            syscall_ptr: felt*,
            pedersen_ptr: HashBuiltin*,
            range_check_ptr
        }(new_admin: felt):
        Proxy_admin.write(new_admin)
        return ()
    end

    #
    # Getters
    #

    func get_implementation{
            syscall_ptr: felt*,
            pedersen_ptr: HashBuiltin*,
            range_check_ptr
        }() -> (implementation: felt):
        let (implementation) = Proxy_implementation_address.read()
        return (implementation)
    end

    func get_admin{
            syscall_ptr: felt*,
            pedersen_ptr: HashBuiltin*,
            range_check_ptr
        }() -> (admin: felt):
        let (admin) = Proxy_admin.read()
        return (admin)
    end

    #
    # Guards
    #

    func assert_only_admin{
            syscall_ptr: felt*,
            pedersen_ptr: HashBuiltin*,
            range_check_ptr
        }():
        let (caller) = get_caller_address()
        let (admin) = Proxy_admin.read()
        with_attr error_message("Proxy: caller is not admin"):
            assert admin = caller
        end
        return ()
    end

end
