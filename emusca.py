#!/usr/bin/env python2

from __future__ import print_function

import binascii as ba
import struct
import sys

from unicorn import *
from unicorn.arm_const import *

from capstone import *
from capstone.arm import *
from xprint import to_hex, to_x_32

import lief

import intervaltree as itree

import numpy as np

import gmpy2

cs_arm = Cs(CS_ARCH_ARM, CS_MODE_ARM)
cs_arm.detail = True
cs_thumb = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
cs_thumb.detail = True

IRAM_START = 0x40000000
IRAM_SIZE = 256 * 1024
RCM_PAYLOAD_ADDR    = 0x4000A000

USB_SEND_ADDR = 0xfff05092
NvOsWaitUS_ADDR = 0xFFF00B9A

PMC_BASE = 0x7000E400
PMC_SCRATCH0 = 0x50
PMC_SCRATCH1 = 0x54

FOURK_ALIGN_MASK = 0xFFFFF000
EVEN_MASK = 0xFFFFFFFE

def roundup(x, m):
	return x if x % m == 0 else x + m - x % m

def rounddown(x, m):
	return x if x % m == 0 else x - x % m

min_sp = 0xFFFFFFFF

reg_ids = (UC_ARM_REG_APSR, UC_ARM_REG_APSR_NZCV, UC_ARM_REG_CPSR, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_SP, UC_ARM_REG_SPSR, UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11, UC_ARM_REG_R12)

reg_names = {
	UC_ARM_REG_APSR: 'APSR',
	UC_ARM_REG_APSR_NZCV: 'APSR_NZCV',
	UC_ARM_REG_CPSR: 'CPSR',
	UC_ARM_REG_LR: 'LR',
	UC_ARM_REG_PC: 'PC',
	UC_ARM_REG_SP: 'SP',
	UC_ARM_REG_SPSR: 'SPSR',
	UC_ARM_REG_R0: 'R0',
	UC_ARM_REG_R1: 'R1',
	UC_ARM_REG_R2: 'R2',
	UC_ARM_REG_R3: 'R3',
	UC_ARM_REG_R4: 'R4',
	UC_ARM_REG_R5: 'R5',
	UC_ARM_REG_R6: 'R6',
	UC_ARM_REG_R7: 'R7',
	UC_ARM_REG_R8: 'R8',
	UC_ARM_REG_R9: 'R9',
	UC_ARM_REG_R10: 'R10',
	UC_ARM_REG_R11: 'R11',
	UC_ARM_REG_R12: 'R12',
}

saved_regs = None
elf = None
ct_sym = None
pt_sym = None
key_sym = None
iv_sym = None
hd_list = []

def print_keystuff(uc):
	key = uc.mem_read(key_sym.value, key_sym.size)
	iv = uc.mem_read(iv_sym.value, iv_sym.size)
	ct = uc.mem_read(ct_sym.value, ct_sym.size)
	pt = uc.mem_read(pt_sym.value, pt_sym.size)
	print("\tkey: {}".format(ba.hexlify(key)))
	print("\tiv: {}".format(ba.hexlify(iv)))
	print("\tct: {}".format(ba.hexlify(ct)))
	print("\tpt: {}".format(ba.hexlify(pt)))

def dump_regs(regs):
	for k in regs.keys():
		print("\t%s:\t0x%08x" % (reg_names[k], regs[k]))

def dump_regs_changed(regs):
	regs.pop(UC_ARM_REG_PC, None)
	for k in regs.keys():
		print("\t%s:\t0x%08x -> 0x%08x" % (reg_names[k], regs[k]['old'], regs[k]['new']))

def all_regs(uc):
	reg_vals = uc.reg_read_batch(reg_ids)
	regs = {}
	for i, reg_val in enumerate(reg_vals):
		regs[reg_ids[i]] = reg_val
	return regs

def changed_regs(old_regs, new_regs):
	regs = {}
	for k in old_regs.keys():
		if old_regs[k] != new_regs[k]:
			regs[k] = {'old': old_regs[k], 'new': new_regs[k]}
			# regs[k] = new_regs[k]
	return regs

def hamming_distance_pair(old_regs, new_regs):
	pass

def hamming_distance_changed(changed_regs):
	keys = changed_regs.keys()
	old = map(lambda r: changed_regs[r]['old'], keys)
	new = map(lambda r: changed_regs[r]['new'], keys)
	# print("old: {} new: {}".format(old, new))
	# xor = np.bitwise_xor(old, new)
	# print("xor: {}".format(xor))
	# popcnt = np.bincount(xor.transpose())
	# print("popcnt: {}".format(popcnt))
	hd = sum(map(lambda p: gmpy2.hamdist(p[0], p[1]), zip(old, new)))
	# print("hd: {}".format(hd))
	return hd


def print_insn_detail(insn, insn_bytes):
    # print address, insn bytes, mnemonic and operands
    insn_bytes_str = ba.hexlify(insn_bytes)
    insn_bytes_str = ' '.join(insn_bytes_str[i:i+2] for i in range(0, len(insn_bytes_str), 2))
    print("0x%x: %s\t%s\t%s" % (insn.address, insn_bytes_str, insn.mnemonic, insn.op_str))

    return

    # "data" instruction generated by SKIPDATA option has no detail
    if insn.id == 0:
        return

    if len(insn.operands) > 0:
        print("\top_count: %u" % len(insn.operands))
        c = 0
        for i in insn.operands:
            if i.type == ARM_OP_REG:
                print("\t\toperands[%u].type: REG = %s" % (c, insn.reg_name(i.reg)))
            if i.type == ARM_OP_IMM:
                print("\t\toperands[%u].type: IMM = 0x%s" % (c, to_x_32(i.imm)))
            if i.type == ARM_OP_PIMM:
                print("\t\toperands[%u].type: P-IMM = %u" % (c, i.imm))
            if i.type == ARM_OP_CIMM:
                print("\t\toperands[%u].type: C-IMM = %u" % (c, i.imm))
            if i.type == ARM_OP_FP:
                print("\t\toperands[%u].type: FP = %f" % (c, i.fp))
            if i.type == ARM_OP_SYSREG:
                print("\t\toperands[%u].type: SYSREG = %u" % (c, i.reg))
            if i.type == ARM_OP_SETEND:
                if i.setend == ARM_SETEND_BE:
                    print("\t\toperands[%u].type: SETEND = be" % c)
                else:
                    print("\t\toperands[%u].type: SETEND = le" % c)
            if i.type == ARM_OP_MEM:
                print("\t\toperands[%u].type: MEM" % c)
                if i.mem.base != 0:
                    print("\t\t\toperands[%u].mem.base: REG = %s" \
                        % (c, insn.reg_name(i.mem.base)))
                if i.mem.index != 0:
                    print("\t\t\toperands[%u].mem.index: REG = %s" \
                        % (c, insn.reg_name(i.mem.index)))
                if i.mem.scale != 1:
                    print("\t\t\toperands[%u].mem.scale: %u" \
                        % (c, i.mem.scale))
                if i.mem.disp != 0:
                    print("\t\t\toperands[%u].mem.disp: 0x%s" \
                        % (c, to_x_32(i.mem.disp)))
                if i.mem.lshift != 0:
                    print("\t\t\toperands[%u].mem.lshift: 0x%s" \
                        % (c, to_x_32(i.mem.lshift)))

            if i.neon_lane != -1:
                print("\t\toperands[%u].neon_lane = %u" % (c, i.neon_lane))

            if i.access == CS_AC_READ:
                print("\t\toperands[%u].access: READ\n" % (c))
            elif i.access == CS_AC_WRITE:
                print("\t\toperands[%u].access: WRITE\n" % (c))
            elif i.access == CS_AC_READ | CS_AC_WRITE:
                print("\t\toperands[%u].access: READ | WRITE\n" % (c))

            if i.shift.type != ARM_SFT_INVALID and i.shift.value:
                print("\t\t\tShift: %u = %u" \
                    % (i.shift.type, i.shift.value))
            if i.vector_index != -1:
                print("\t\t\toperands[%u].vector_index = %u" %(c, i.vector_index))
            if i.subtracted:
                print("\t\t\toperands[%u].subtracted = True" %c)

            c += 1

    if insn.update_flags:
        print("\tUpdate-flags: True")
    if insn.writeback:
        print("\tWrite-back: True")
    if not insn.cc in [ARM_CC_AL, ARM_CC_INVALID]:
        print("\tCode condition: %u" % insn.cc)
    if insn.cps_mode:
        print("\tCPSI-mode: %u" %(insn.cps_mode))
    if insn.cps_flag:
        print("\tCPSI-flag: %u" %(insn.cps_flag))
    if insn.vector_data:
        print("\tVector-data: %u" %(insn.vector_data))
    if insn.vector_size:
        print("\tVector-size: %u" %(insn.vector_size))
    if insn.usermode:
        print("\tUser-mode: True")
    if insn.mem_barrier:
        print("\tMemory-barrier: %u" %(insn.mem_barrier))

    (regs_read, regs_write) = insn.regs_access()

    if len(regs_read) > 0:
        print("\tRegisters read:", end="")
        for r in regs_read:
            print(" %s" %(insn.reg_name(r)), end="")
        print("")

    if len(regs_write) > 0:
        print("\tRegisters modified:", end="")
        for r in regs_write:
            print(" %s" %(insn.reg_name(r)), end="")
        print("")

def findsym(elf, name):
	return next(s for s in elf.symbols if s.name == name)

# callback for tracing basic blocks
def hook_block(uc, address, size, user_data):
	print(">>> Tracing basic block at 0x%x, block size = 0x%x" %(address, size))

# callback for tracing instructions
def hook_code(uc, address, size, user_data):
	global saved_regs, min_sp
	new_regs = all_regs(uc)
	min_sp = min(min_sp, new_regs[UC_ARM_REG_SP])
	ch_regs = changed_regs(saved_regs, new_regs)
	# ch_regs.pop(UC_ARM_REG_PC, None)
	hamming_distance_changed(ch_regs)
	dump_regs_changed(ch_regs)
	is_thumb = new_regs[UC_ARM_REG_CPSR] & (1 << 5) != 0
	mode_str = None
	if is_thumb:
		mode_str = "Thumb"
	else:
		mode_str = "ARM"
	print(">>> Tracing instruction at 0x%x, instruction size = 0x%x, mode: %s" % (address, size, mode_str))
	pc = new_regs[UC_ARM_REG_PC]
	insn_bytes = uc.mem_read(pc, size)
	insn = None
	if is_thumb:
		insn = list(cs_thumb.disasm(insn_bytes, pc))[0]
	else:
		insn = list(cs_arm.disasm(insn_bytes, pc))[0]
	print_insn_detail(insn, insn_bytes)
	saved_regs = new_regs

def hook_code_hamming_distance(uc, address, size, user_data):
	global saved_regs, min_sp
	new_regs = all_regs(uc)
	min_sp = min(min_sp, new_regs[UC_ARM_REG_SP])
	ch_regs = changed_regs(saved_regs, new_regs)
	# ch_regs.pop(UC_ARM_REG_PC, None)
	hd = hamming_distance_changed(ch_regs)
	hd_list.append(hd)
	saved_regs = new_regs

def hook_usb_send(uc, address, size, user_data):
	global saved_regs
	print(">>> Hooking usb_send")
	(buf_ptr, size, size_sent_ptr, lr) = uc.reg_read_batch((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_LR))
	print(">>> \tbuf_ptr: 0x%08x, size: 0x%08x, size_sent_ptr: 0x%08x lr: 0x%08x" % (buf_ptr, size, size_sent_ptr, lr))
	buf = uc.mem_read(buf_ptr, size)
	try:
		buf_str = buf.decode('utf-8')
		print(">>> \tbuf:\t'%s' %s" % (buf_str, ba.hexlify(buf)))
	except UnicodeError:
		print(">>> \tbuf:\t%s" % ba.hexlify(buf))
	size_sent = struct.pack('<I', size)
	uc.mem_write(size_sent_ptr, size_sent)
	new_regs = all_regs(uc)
	ch_regs = changed_regs(saved_regs, new_regs)
	ch_regs.pop(UC_ARM_REG_PC, None)
	dump_regs_changed(ch_regs)
	saved_regs = new_regs
	# uc.setdbg()
	uc.reg_write(UC_ARM_REG_R0, 0)
	uc.reg_write(UC_ARM_REG_PC, lr)

def hook_NvOsWaitUS(uc, address, size, user_data):
	print(">>> Hooking NvOsWaitUS")
	(us, lr) = uc.reg_read_batch((UC_ARM_REG_R0, UC_ARM_REG_LR))
	print(">>> \tus: %d lr: 0x%08x" % (us, lr))
	uc.reg_write(UC_ARM_REG_PC, lr)

def hook_exit(uc, address, size, user_data):
	print(">>> Hooking _exit")
	(retval, lr, pc) = uc.reg_read_batch((UC_ARM_REG_R0, UC_ARM_REG_LR, UC_ARM_REG_PC))
	print(">>> \tretval: %d lr: 0x%08x address: 0x%08X pc: 0x%08x size: %d" % (retval, lr, address, pc, size))
	print_keystuff(uc)
	uc.emu_stop()

# callback for tracing invalid memory access (READ or WRITE)
def hook_mem_unmapped(uc, access, address, size, value, user_data):
	if access == UC_MEM_WRITE_UNMAPPED:
		print(">>> Missing memory is being WRITE at 0x%08x, data size = %u, data value = 0x%08x" \
				%(address, size, value))
	else:
		print(">>> Missing memory is being READ at 0x%08x, data size = %u, data value = 0x%08x" \
				%(address, size, value))
	# return False to indicate we want to stop emulation
	return False

# callback for tracing memory access (READ or WRITE)
def hook_mem_access(uc, access, address, size, value, user_data):
	old_val_bytes = uc.mem_read(address, size)
	old_val_bytes = uc.mem_read(address, size)
	struct_fmt = {1: '<B', 2: '<H', 4: '<I'}[size]
	old_val = struct.unpack(struct_fmt, old_val_bytes)[0]
	if access == UC_MEM_WRITE:
		print(">>> Memory is being WRITE at 0x%08x, data size = %u, data value = 0x%08x, old value = 0x%08x" \
				%(address, size, value, old_val))
		if address == PMC_BASE and value & (1 << 4) != 0:
			print(">>> PMC reset issued, stopping emu")
			uc.emu_stop()
		if address == PMC_BASE + PMC_SCRATCH0:
			print(">>> PMC_SCRATCH0 written")
		if address == PMC_BASE + PMC_SCRATCH1:
			print(">>> PMC_SCRATCH1 written")
	else:   # READ
		print(">>> Memory is being READ at 0x%08x, data size = %u, data value = 0x%08x" \
				%(address, size, old_val))

def main(argv):
	global saved_regs, elf, key_sym, iv_sym, ct_sym, pt_sym

	elf = lief.parse(sys.argv[1])
	tree = itree.IntervalTree()
	for seg in elf.segments:
		rstart = rounddown(seg.virtual_address, 4096)
		rend = roundup(seg.virtual_address + seg.virtual_size, 4096)
		rsize_content = roundup(seg.virtual_size, 4096)
		rsize_range = rend - rstart
		zpad_front = b'\x00' * (seg.virtual_address - rstart)
		zpad_back = b'\x00' * (rend - (seg.virtual_address + seg.virtual_size))
		print("rstart: 0x{:08x} rend: 0x{:08x} rsize_content: 0x{:08x} rsize_content: 0x{:08x}".format(rstart, rend, rsize_content, rsize_range))
		assert(not tree.overlaps(rstart, rend))
		tree[rstart:rend] = zpad_front + bytes(bytearray(seg.content)) + zpad_back
	minaddr = tree.begin()
	maxaddr = tree.end()
	print("minaddr: 0x{:08x} maxaddr: 0x{:08x}".format(minaddr, maxaddr))
	stack_gap = 1024*1024
	stack_size = 1024*1024
	stack_begin = tree.end() + stack_gap
	stack_end = stack_begin + stack_size
	tree[stack_begin:stack_end] = b'\x00' * stack_size
	stack_top = stack_end - 4
	# print("tree: {}".format(tree))
	minaddr = tree.begin()
	maxaddr = tree.end()
	print("minaddr: 0x{:08x} maxaddr: 0x{:08x}".format(minaddr, maxaddr))

	print("Emulate thumb code")
	try:
		# Initialize emulator in thumb mode
		uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)

		for seg in tree:
			uc.mem_map(seg.begin, seg.end - seg.begin)
			uc.mem_write(seg.begin, seg.data)

		uc.reg_write(UC_ARM_REG_SP, stack_top)
		uc.reg_write(UC_ARM_REG_APSR, 0xFFFFFFFF) #All application flags turned on

		saved_regs = all_regs(uc)

		# uc.hook_add(UC_HOOK_BLOCK, hook_block)
		# uc.hook_add(UC_HOOK_CODE, hook_code)
		uc.hook_add(UC_HOOK_CODE, hook_code_hamming_distance)
		# uc.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, hook_mem_access)
		# uc.hook_add(UC_HOOK_MEM_UNMAPPED, hook_mem_unmapped)

		key_sym = findsym(elf, 'key')
		iv_sym = findsym(elf, 'iv')
		ct_sym = findsym(elf, 'ct')
		pt_sym = findsym(elf, 'pt')
		exit_sym = findsym(elf, '_exit')
		uc.hook_add(UC_HOOK_CODE, hook_exit, begin=exit_sym.value & EVEN_MASK, end=(exit_sym.value & EVEN_MASK) + 2)

		print_keystuff(uc)

		# uc.emu_start(RCM_PAYLOAD_ADDR, len(binbuf))
		uc.emu_start(elf.entrypoint, maxaddr)

		print("Minimum SP: 0x%08x" % min_sp)
		print("len(hd_list): {}".format(len(hd_list)))
		np.save('hd.npy', hd_list)

	except UcError as e:
		print("ERROR: %s" % e)

if __name__ == "__main__":
	sys.exit(main(sys.argv))
