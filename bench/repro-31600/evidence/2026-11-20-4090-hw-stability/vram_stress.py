import torch, time, sys, datetime

DUR_S = 25 * 60
VERIFY_EVERY = 600

def log(m):
    print("[%s] %s" % (datetime.datetime.now().isoformat(timespec="seconds"), m), flush=True)

def expected(i, n):
    idx = torch.arange(n, dtype=torch.int64, device="cuda")
    v = (idx * 2654435761 + 1013904223) & 0xFFFFFFFF
    return v + ((i & 0xFFFFFFFF) << 32)

def verify(blocks):
    bad = []
    for i, b in enumerate(blocks):
        if not torch.equal(b, expected(i, b.numel())):
            ne = (b != expected(i, b.numel())).nonzero()
            log("CORRUPTION block=%d count=%d first=%s" % (i, ne.numel(), ne[:5].flatten().tolist()))
            bad.append(i)
    return bad

def main():
    free, total = torch.cuda.mem_get_info()
    log("device=%s total=%.2fGiB free=%.2fGiB" % (torch.cuda.get_device_name(0), total/2**30, free/2**30))
    a = torch.empty(1 << 32, dtype=torch.uint8, device="cuda"); a.fill_(0xAB)
    b = torch.empty(1 << 32, dtype=torch.uint8, device="cuda")
    b.copy_(a); torch.cuda.synchronize()
    t0 = time.time(); b.copy_(a); torch.cuda.synchronize(); dt = time.time() - t0
    log("D2D copy 4GiB: %.3fs -> %.1f GB/s effective (8GiB traffic)" % (dt, 8*2**30/dt/1e9))
    del a, b; torch.cuda.empty_cache()
    free, _ = torch.cuda.mem_get_info()
    keep = int(2.5 * (1 << 30)); gemm = int(0.75 * (1 << 30))
    nblocks = max(1, int((free - keep - gemm) // (1 << 30)))
    log("filling %d blocks x 1GiB (int64 pattern)" % nblocks)
    blocks = []
    for i in range(nblocks):
        blocks.append(expected(i, (1 << 30) // 8))
    torch.cuda.synchronize(); log("fill done")
    N = 8192
    A = torch.randn(N, N, dtype=torch.float32, device="cuda")
    Bm = torch.randn(N, N, dtype=torch.float32, device="cuda")
    C = torch.empty(N, N, dtype=torch.float32, device="cuda")
    t0 = time.time(); last = time.time(); it = 0
    while time.time() - t0 < DUR_S:
        for b in blocks:
            b.sum().item()
        torch.matmul(A, Bm, out=C)
        it += 1
        if time.time() - last >= VERIFY_EVERY:
            log("mid verify @iter %d ..." % it)
            if verify(blocks):
                log("MID-VERIFY FAILED"); sys.exit(1)
            last = time.time()
    if verify(blocks):
        log("FINAL VERIFY FAILED"); sys.exit(1)
    log("rounds=%d ALL PASS" % it)

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log("EXCEPTION: %r" % (e,)); sys.exit(2)
