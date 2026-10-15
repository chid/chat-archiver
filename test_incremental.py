"""Property test: feeding a transcript in slices must equal a one-shot rebuild."""
import glob, os, shutil, subprocess, sys, difflib
S = os.environ.get("TESTDIR", "/tmp/chat-archiver-test")
CASES = [[40,140],[10,25,60,90,200],[5,400],[70,71,72,500],[1,2,3,4,5,300],[2,3]]

def run(home, out, *extra):
    subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive.py"),
                    "--out", out, "--quiet", *extra],
                   env={**os.environ, "HOME": home}, capture_output=True)

def body(d):
    f = glob.glob(d + "/**/*.md", recursive=True)
    return open(f[0]).read() if f else None

def main():
    srcs = [s for s in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
            if os.path.getsize(s) > 150_000]
    srcs = sorted(srcs, key=os.path.getsize)[:4]
    ok = bad = 0
    for src in srcs:
        lines = open(src, errors="replace").readlines()
        for cuts in CASES:
            for d in ("fh", "inc", "ref"):
                shutil.rmtree(f"{S}/{d}", ignore_errors=True)
            os.makedirs(f"{S}/fh/.claude/projects/p")
            T = f"{S}/fh/.claude/projects/p/aaaaaaaa-1111-2222-3333-444444444444.jsonl"
            prev = 0
            for c in cuts:
                with open(T, "a") as fh:
                    fh.writelines(lines[prev:c])
                prev = c
                run(f"{S}/fh", f"{S}/inc", "--tools", "full")
            with open(T, "a") as fh:
                fh.writelines(lines[prev:])
            run(f"{S}/fh", f"{S}/inc", "--tools", "full")
            run(f"{S}/fh", f"{S}/ref", "--tools", "full")
            a, b = body(f"{S}/inc"), body(f"{S}/ref")
            if a == b:
                ok += 1
            else:
                bad += 1
                print(f"  FAIL {os.path.basename(src)[:8]} cuts={cuts}")
                for l in list(difflib.unified_diff((a or "").splitlines(),
                                                   (b or "").splitlines(), lineterm=""))[2:10]:
                    print("       ", l[:100])
    print(f"\n{ok} identical, {bad} mismatched  ({len(srcs)} transcripts x {len(CASES)} cut patterns)")
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
