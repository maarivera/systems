"""Syntax and readiness checks used by the Automation Hub server.

Everything here is read-only. Files are checked from the database; when an external
tool needs real files they are written to a private temporary folder. Nothing is run
against hosts or cloud APIs: no SSH connections, no terraform plan/apply.

Checkers by file type
    .sh / bash shebang   bash -n (+ shellcheck when installed)
    .py / python shebang Python's compiler (syntax errors and syntax warnings)
    .yml / .yaml         YAML parser; playbooks and rulebooks also get structure checks
    .tf                  terraform fmt (bracket check when terraform is not installed)
    .hcl                 bracket check
    .json                JSON parser
    .j2                  Jinja2 parser
    .cfg                 INI parser (ansible.cfg)
    inventory .ini       ansible-inventory (INI parser when Ansible is not installed)
Inventories
    host limits          resolve_limit() follows Ansible host patterns: names, groups, globs, ~regex, ! and &
    YAML inventories     yaml_inventory_ini() converts them to INI, the format the hub stores
Per automation
    Ansible playbook     ansible-playbook --syntax-check on the entry playbook
    Terraform module     terraform validate, when the module folder has been initialised
"""
import configparser
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import warnings
from pathlib import Path

try:
    import yaml
except ImportError:  # optional: Ansible's own Python is used instead
    yaml = None
try:
    import jinja2
except ImportError:
    jinja2 = None

TIMEOUT = 90
_cache = {}
_lock = threading.Lock()
_py_lock = threading.Lock()  # warnings capture is process-wide


def msg(line, level, text, **extra):
    return {"line": line, "level": level, "text": str(text).strip()[:2000], **extra}


def status_of(messages, default="ok"):
    levels = {m["level"] for m in messages}
    return "error" if "error" in levels else "warning" if "warning" in levels else default


def worst(statuses):
    for s in ("error", "warning", "ok"):
        if s in statuses:
            return s
    return "ok"


def run(cmd, cwd=None, stdin=None, env=None):
    return subprocess.run(cmd, cwd=cwd, input=stdin, capture_output=True, text=True,
                          timeout=TIMEOUT, env={**os.environ, **(env or {})})


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"


_versions = None


def tools():
    """Installed tools and their versions (None when missing). Cached for the server's lifetime."""
    global _versions
    if _versions is None:
        out = {}
        for name, args in (("bash", ["--version"]), ("python3", ["--version"]), ("ansible-playbook", ["--version"]),
                           ("ansible-rulebook", ["--version"]), ("terraform", ["version"]), ("shellcheck", ["--version"])):
            path = shutil.which(name)
            if not path:
                out[name] = None
                continue
            try:
                r = run([path, *args])
                lines = [l.strip() for l in (r.stdout or r.stderr).splitlines() if l.strip()]
                out[name] = next((l for l in lines if l.lower().startswith("version")), lines[0] if lines else "installed")[:90]
            except (OSError, subprocess.SubprocessError):
                out[name] = "installed"
        _versions = out
    return _versions


# ------------------------------------------------------------------- YAML
if yaml:
    class _AnyTagLoader(yaml.SafeLoader):
        """SafeLoader that accepts Ansible tags such as !vault and !unsafe."""
    _AnyTagLoader.add_multi_constructor("!", lambda loader, suffix, node: None)

_YAML_HELPER = r"""
import json, sys, yaml
class L(yaml.SafeLoader): pass
L.add_multi_constructor("!", lambda l, s, n: None)
try:
    print(json.dumps({"ok": True, "docs": list(yaml.load_all(sys.stdin.read(), Loader=L))}, default=str))
except yaml.MarkedYAMLError as e:
    m = e.problem_mark
    print(json.dumps({"ok": False, "line": m.line + 1 if m else None, "error": " ".join(filter(None, [e.problem, e.context]))}))
"""


def _ansible_python():
    """The interpreter from ansible-playbook's shebang; it always has PyYAML."""
    path = shutil.which("ansible-playbook")
    try:
        first = Path(path).read_text(errors="replace").splitlines()[0] if path else ""
    except (OSError, IndexError):
        return None
    if not first.startswith("#!"):
        return None
    parts = first[2:].split()
    if parts and parts[0].endswith("/env"):
        parts = parts[1:]
    return parts or None


def parse_yaml(content):
    """(documents, messages); messages is None when no YAML parser is available."""
    if yaml:
        try:
            return list(yaml.load_all(content, Loader=_AnyTagLoader)), []
        except yaml.MarkedYAMLError as e:
            m = e.problem_mark
            return None, [msg(m.line + 1 if m else None, "error", " ".join(filter(None, [e.problem, e.context])) or e)]
        except yaml.YAMLError as e:
            return None, [msg(None, "error", e)]
    py = _ansible_python()
    if py:
        try:
            out = json.loads(run([*py, "-c", _YAML_HELPER], stdin=content).stdout)
            return (out["docs"], []) if out["ok"] else (None, [msg(out["line"], "error", out["error"])])
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            pass
    return None, None


def playbook_structure(docs):
    plays = docs[0] if docs else None
    if plays is None:
        return [msg(1, "error", "The playbook is empty")]
    if not isinstance(plays, list):
        return [msg(1, "error", "A playbook must be a list of plays")]
    out = []
    for i, p in enumerate(plays, 1):
        if not isinstance(p, dict):
            out.append(msg(None, "error", f"Play {i} is not a mapping"))
        elif not any(k in p for k in ("hosts", "import_playbook", "ansible.builtin.import_playbook")):
            out.append(msg(None, "error", f"Play {i} ({p.get('name', 'unnamed')}) has no hosts"))
    return out


def rulebook_structure(docs):
    sets = docs[0] if docs else None
    if not isinstance(sets, list) or not sets:
        return [msg(1, "error", "A rulebook must be a list of rulesets")]
    out = []
    for i, rs in enumerate(sets, 1):
        if not isinstance(rs, dict):
            out.append(msg(None, "error", f"Ruleset {i} is not a mapping"))
            continue
        name = rs.get("name", f"Ruleset {i}")
        out += [msg(None, "error", f'{name}: missing "{k}"') for k in ("hosts", "sources", "rules") if k not in rs]
        for j, rule in enumerate(rs.get("rules") or [], 1):
            if isinstance(rule, dict):
                if "condition" not in rule:
                    out.append(msg(None, "error", f'{name}, rule {j}: missing "condition"'))
                if "action" not in rule and "actions" not in rule:
                    out.append(msg(None, "error", f'{name}, rule {j}: missing "action"'))
    return out


# ------------------------------------------------------------ file checks
def check_shell(content):
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(content)
    try:
        r = run(["bash", "-n", f.name])
        msgs = []
        for line in r.stderr.replace(f.name + ": ", "").splitlines():
            m = re.match(r"line (\d+): (.*)", line.strip())
            text = m[2] if m else line.strip()
            if msgs and text.startswith("`"):  # bash echoes the offending line after the error
                msgs[-1]["text"] += "  " + text
            elif text:
                msgs.append(msg(int(m[1]) if m else None, "error", text))
        if r.returncode and not msgs:
            msgs.append(msg(None, "error", "bash -n failed"))
        tool = "bash -n"
        sc = shutil.which("shellcheck")
        if sc:
            try:
                data = json.loads(run([sc, "-f", "json1", "-S", "warning", f.name]).stdout or '{"comments": []}')
                msgs += [msg(c["line"], "error" if c["level"] == "error" else "warning", f"SC{c['code']}: {c['message']}")
                         for c in data.get("comments", [])]
                tool += " + shellcheck"
            except (ValueError, subprocess.SubprocessError):
                pass
        return {"status": status_of(msgs), "tool": tool, "messages": msgs}
    finally:
        os.unlink(f.name)


def check_python(content, path):
    msgs = []
    with _py_lock, warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            compile(content, path, "exec", dont_inherit=True)
        except SyntaxError as e:
            msgs.append(msg(e.lineno, "error", f"{type(e).__name__}: {e.msg}"))
        except ValueError as e:
            msgs.append(msg(None, "error", e))
    msgs += [msg(w.lineno, "warning", w.message) for w in caught if issubclass(w.category, SyntaxWarning)]
    return {"status": status_of(msgs), "tool": "python compile", "messages": msgs}


def check_yaml(content, role):
    docs, msgs = parse_yaml(content)
    if msgs is None:
        return {"status": "skipped", "tool": None, "messages": [msg(None, "info", "No YAML parser available (pip install pyyaml)")]}
    tool = "yaml"
    if docs is not None and role == "playbook" and not shutil.which("ansible-playbook"):
        msgs += playbook_structure(docs)
        tool += " + playbook structure"
    if docs is not None and role == "rulebook":
        msgs += rulebook_structure(docs)
        tool += " + rulebook structure"
    return {"status": status_of(msgs), "tool": tool, "messages": msgs}


def parse_tf_diags(text):
    out = []
    for m in re.finditer(r"(Error|Warning): (.+?)\n(.*?)(?=\n(?:Error|Warning): |\Z)", text, re.S):
        body = m[3]
        line = re.search(r"line (\d+)", body)
        detail = " ".join(l.strip() for l in body.strip().split("\n\n")[-1].splitlines()
                          if l.strip() and not re.match(r"^\s*(on |\d+:)", l))
        out.append(msg(int(line[1]) if line else None, m[1].lower(), m[2] + (f": {detail}" if detail else "")))
    return out or [msg(None, "error", text.strip()[:500] or "terraform fmt failed")]


def hcl_balance(content):
    """Bracket, string and heredoc balance for HCL when terraform isn't installed."""
    pairs, stack, out = {")": "(", "]": "[", "}": "{"}, [], []
    heredoc, block_comment = None, False
    for ln, text in enumerate(content.split("\n"), 1):
        if heredoc:
            if text.strip() == heredoc:
                heredoc = None
            continue
        j, in_str = 0, False
        while j < len(text):
            ch = text[j]
            if block_comment:
                if text.startswith("*/", j):
                    block_comment, j = False, j + 2
                    continue
            elif in_str:
                if ch == "\\":
                    j += 1
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "#" or text.startswith("//", j):
                break
            elif text.startswith("/*", j):
                block_comment, j = True, j + 2
                continue
            elif text.startswith("<<", j) and re.match(r"<<-?\s*[A-Za-z_]\w*", text[j:]):
                heredoc = re.match(r"<<-?\s*([A-Za-z_]\w*)", text[j:])[1]
                break
            elif ch in "([{":
                stack.append((ch, ln))
            elif ch in ")]}":
                if not stack or stack[-1][0] != pairs[ch]:
                    return [msg(ln, "error", f"Unexpected '{ch}'")]
                stack.pop()
            j += 1
        if in_str:
            return [msg(ln, "error", "Unterminated string")]
    if block_comment:
        out.append(msg(None, "error", "Unterminated /* comment"))
    if heredoc:
        out.append(msg(None, "error", f"Heredoc <<{heredoc} is never closed"))
    if stack:
        out.append(msg(stack[-1][1], "error", f"'{stack[-1][0]}' is never closed"))
    return out


def check_terraform(content):
    tf = shutil.which("terraform")
    if not tf:
        msgs = hcl_balance(content)
        return {"status": status_of(msgs), "tool": "bracket check (terraform not installed)", "messages": msgs}
    r = run([tf, "fmt", "-no-color", "-"], stdin=content)
    if r.returncode:
        msgs = parse_tf_diags(r.stderr)
        return {"status": status_of(msgs), "tool": "terraform fmt", "messages": msgs}
    msgs = [] if r.stdout == content else [msg(None, "info", "Formatting differs from terraform fmt")]
    return {"status": "ok", "tool": "terraform fmt", "messages": msgs}


def check_json(content):
    try:
        json.loads(content)
        msgs = []
    except json.JSONDecodeError as e:
        msgs = [msg(e.lineno, "error", e.msg)]
    return {"status": status_of(msgs), "tool": "json", "messages": msgs}


def check_jinja(content):
    if jinja2 is None:
        return {"status": "skipped", "tool": None, "messages": [msg(None, "info", "jinja2 is not installed")]}
    try:
        jinja2.Environment(extensions=["jinja2.ext.do", "jinja2.ext.loopcontrols"]).parse(content)
        msgs = []
    except jinja2.TemplateSyntaxError as e:
        msgs = [msg(e.lineno, "error", e.message)]
    return {"status": status_of(msgs), "tool": "jinja2", "messages": msgs}


def check_cfg(content):
    p = configparser.ConfigParser(interpolation=None, strict=False, allow_no_value=True)
    try:
        p.read_string(content)
        msgs = []
    except configparser.MissingSectionHeaderError as e:
        msgs = [msg(e.lineno, "error", "Settings must be inside a [section]")]
    except configparser.ParsingError as e:
        msgs = [msg(n, "error", f"Cannot parse: {line.strip()}") for n, line in e.errors]
    return {"status": status_of(msgs), "tool": "ini", "messages": msgs}


def inventory_hosts(content):
    """Parse an Ansible INI inventory into ({group: [hosts]}, {group: [child groups]})."""
    groups, children, cur, mode = {"ungrouped": []}, {}, "ungrouped", "hosts"
    for raw in content.splitlines():
        line = re.split(r"\s#", raw.strip(), 1)[0].strip()
        if not line or line[0] in "#;":
            continue
        m = re.match(r"^\[([^\]:]+)(?::(\w+))?\]$", line)
        if m:
            cur, mode = m[1], m[2] or "hosts"
            groups.setdefault(cur, [])
        elif mode == "hosts":
            groups.setdefault(cur, []).append(line.split()[0])
        elif mode == "children":
            children.setdefault(cur, []).append(line.split()[0])
    return groups, children


def hosts_in(groups, children, group=None, seen=None):
    if group is None:
        return sorted({h for hs in groups.values() for h in hs})
    seen = seen or set()
    if group in seen:
        return []
    seen.add(group)
    out = set(groups.get(group, []))
    for child in children.get(group, []):
        out.update(hosts_in(groups, children, child, seen))
    return sorted(out)


def limit_terms(limit):
    """Split an Ansible host pattern (as given to --limit) into its terms."""
    terms = []
    for part in re.split(r"[,\s]+", limit or ""):
        # ':' also separates terms, except inside IPv6 addresses
        terms.extend(part.split(":") if part.count(":") == 1 else [part])
    return [t for t in terms if t]


def resolve_limit(limit, groups, children):
    """(hosts the pattern selects, terms that match nothing). Supports names, groups, globs, ~regex, ! and &."""
    every = set(hosts_in(groups, children))
    names = set(groups) | set(children)

    def match(term):
        if term in ("all", "*"):
            return set(every)
        if term in names:
            return set(hosts_in(groups, children, term))
        if term.startswith("~"):
            try:
                rx = re.compile(term[1:])
            except re.error:
                return set()
            return {h for h in every if rx.search(h)}
        if any(ch in term for ch in "*?["):
            out = {h for h in every if fnmatch.fnmatchcase(h, term)}
            for g in names:
                if fnmatch.fnmatchcase(g, term):
                    out.update(hosts_in(groups, children, g))
            return out
        return {term} & every

    terms = limit_terms(limit)
    unknown = [t.lstrip("!&") for t in terms if not match(t.lstrip("!&"))]
    plain = [t for t in terms if t[0] not in "!&"]
    selected = set().union(*(match(t) for t in plain)) if plain else set(every)
    for t in terms:
        if t[0] == "&":
            selected &= match(t[1:])
        elif t[0] == "!":
            selected -= match(t[1:])
    return sorted(selected), unknown


def limit_issues(limit, content, where):
    """Warnings for a node's host limit checked against the inventory it runs with."""
    if not (limit or "").strip():
        return []
    groups, children = inventory_hosts(content)
    selected, unknown = resolve_limit(limit, groups, children)
    issues = [msg(None, "warning", f'"{t}" in the host limit is not a host or group in {where}') for t in unknown]
    if not selected:
        issues.append(msg(None, "error", f"The host limit matches no hosts in {where}, so nothing would run"))
    return issues


def _ini_value(v):
    if isinstance(v, str):
        return v if v and not re.search(r"[\s\"'#=]", v) else json.dumps(v)
    return json.dumps(v)


def yaml_inventory_ini(content):
    """Convert an Ansible YAML inventory to INI, so the hub stores one format."""
    docs, msgs = parse_yaml(content)
    if docs is None:
        raise ValueError(msgs[0]["text"] if msgs else "No YAML parser available on the server (pip install pyyaml)")
    data = docs[0] if docs else None
    if not isinstance(data, dict):
        raise ValueError("This is not an Ansible YAML inventory (expected groups such as all: hosts: ...)")
    sections = {}

    def walk(name, g):
        if g is not None and not isinstance(g, dict):
            raise ValueError(f"Group {name} must be a mapping with hosts, children or vars")
        s = sections.setdefault(name, {"hosts": {}, "children": [], "vars": {}})
        g = g or {}
        hosts = g.get("hosts") or {}
        if isinstance(hosts, list):
            hosts = {h: None for h in hosts}
        for h, hv in hosts.items():
            s["hosts"][str(h)] = hv if isinstance(hv, dict) else {}
        s["vars"].update(g.get("vars") or {})
        for child, cg in (g.get("children") or {}).items():
            if str(child) not in s["children"]:
                s["children"].append(str(child))
            walk(str(child), cg)

    for name, g in data.items():
        walk(str(name), g)
    line = lambda h, hv: " ".join([h, *(f"{k}={_ini_value(v)}" for k, v in hv.items())])
    out = ["# Converted from a YAML inventory by Automation Hub"]
    top = sections.pop("all", None)
    if top and top["hosts"]:
        out += [line(h, hv) for h, hv in top["hosts"].items()]
    for name, s in sections.items():
        if s["hosts"] or not (s["children"] or s["vars"]):
            out += ["", f"[{name}]", *(line(h, hv) for h, hv in s["hosts"].items())]
        if s["children"]:
            out += ["", f"[{name}:children]", *s["children"]]
        if s["vars"]:
            out += ["", f"[{name}:vars]", *(f"{k}={_ini_value(v)}" for k, v in s["vars"].items())]
    if top and top["vars"]:
        out += ["", "[all:vars]", *(f"{k}={_ini_value(v)}" for k, v in top["vars"].items())]
    return "\n".join(out) + "\n"


def check_inventory(content):
    msgs = []
    tool = "ini"
    ai = shutil.which("ansible-inventory")
    if ai:
        with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as f:
            f.write(content)
        try:
            r = run([ai, "-i", f.name, "--list"], env={"ANSIBLE_INVENTORY_UNPARSED_FAILED": "1", "ANSIBLE_INVENTORY_ENABLED": "ini",
                                                       "ANSIBLE_NOCOLOR": "1", "ANSIBLE_DEPRECATION_WARNINGS": "0"})
            tool = "ansible-inventory"
            if r.returncode:
                text = r.stderr.replace(f.name, "inventory")
                found = re.findall(r"(?:line (\d+)[^\n]*?:?\s*)?(?:\[WARNING\]:|ERROR!|\[ERROR\]:)\s*(.+)", text)
                line = re.search(r"line (\d+)", text)
                msgs.append(msg(int(line[1]) if line else None, "error", found[-1][1] if found else text.strip()[-400:]))
        except subprocess.SubprocessError as e:
            msgs.append(msg(None, "warning", f"ansible-inventory did not finish: {e}"))
        finally:
            os.unlink(f.name)
    if not ai or msgs:
        # Point at the exact line; ansible-inventory only says that nothing could be parsed.
        headers = [msg(n, "error", f"Malformed group header: {raw.strip()}") for n, raw in enumerate(content.splitlines(), 1)
                   if raw.strip().startswith("[") and not re.match(r"^\[[^\]]+\]\s*(#.*)?$", raw.strip())]
        msgs = headers or msgs
    if not msgs and not hosts_in(*inventory_hosts(content)):
        msgs.append(msg(None, "info", "No hosts defined yet"))
    return {"status": status_of(msgs), "tool": tool, "messages": msgs}


def _check_file(path, content, role):
    name = path.rsplit("/", 1)[-1]
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    first = content.split("\n", 1)[0]
    shebang = first if first.startswith("#!") else ""
    if suffix == ".py" or "python" in shebang:
        return check_python(content, path)
    if suffix in (".sh", ".bash") or re.search(r"\b(ba)?sh\b", shebang):
        return check_shell(content)
    if suffix in (".yml", ".yaml"):
        return check_yaml(content, role)
    if suffix == ".tf":
        return check_terraform(content)
    if suffix == ".hcl":
        msgs = hcl_balance(content)
        return {"status": status_of(msgs), "tool": "bracket check", "messages": msgs}
    if suffix == ".json":
        return check_json(content)
    if suffix == ".j2":
        return check_jinja(content)
    if suffix == ".ini" and ("inventory/" in path or name.startswith("hosts")):
        return check_inventory(content)
    if suffix in (".cfg", ".ini"):
        return check_cfg(content)
    return {"status": "skipped", "tool": None, "messages": []}


def check_file(path, content, role=None):
    """Check one file; results are cached by path, role and content."""
    key = (path, role, hashlib.sha256(content.encode()).hexdigest())
    with _lock:
        hit = _cache.get(key)
    if hit is None:
        try:
            hit = _check_file(path, content, role)
        except (OSError, subprocess.SubprocessError) as e:
            hit = {"status": "skipped", "tool": None, "messages": [msg(None, "info", f"Checker could not run: {e}")]}
        with _lock:
            if len(_cache) > 5000:
                _cache.clear()
            _cache[key] = hit
    return {"path": path, **hit}


# ------------------------------------------------------- automation checks
def write_tree(files, root):
    root = Path(root).resolve()
    for f in files:
        p = (root / f["path"]).resolve()
        if root in p.parents:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f["content"])


def ansible_syntax(files, entry):
    with tempfile.TemporaryDirectory(prefix="hub-check-") as d:
        write_tree(files, d)
        r = run([shutil.which("ansible-playbook"), "--syntax-check", "-i", "localhost,", entry], cwd=d,
                env={"ANSIBLE_NOCOLOR": "1", "ANSIBLE_FORCE_COLOR": "0", "ANSIBLE_RETRY_FILES_ENABLED": "0",
                     "ANSIBLE_DEPRECATION_WARNINGS": "0", "ANSIBLE_LOCALHOST_WARNING": "0"})
        real = str(Path(d).resolve())
        text = (r.stderr + "\n" + r.stdout).replace(real + "/", "").replace(d + "/", "").strip()
    msgs = []
    if r.returncode:
        first = next((l for l in text.splitlines() if re.match(r"^(ERROR!|\[ERROR\]:)", l)), None)
        summary = re.sub(r"^(ERROR!|\[ERROR\]:)\s*", "", first) if first else (text.splitlines() or ["syntax check failed"])[-1]
        loc = re.search(r"appears to be in '([^']+)': line (\d+)", text) or re.search(r"Origin: ([^\s:]+):(\d+)", text)
        if "couldn't resolve module/action" in text or "could not resolve module" in text.lower():
            summary += " (a collection is probably not installed: ansible-galaxy collection install -r requirements.yml)"
        msgs.append(msg(int(loc[2]) if loc else None, "error", summary, file=loc[1] if loc else entry, detail=text[-1500:]))
    else:
        for w in re.findall(r"\[WARNING\]:\s*(.+)", text):
            if not re.search(r"hosts list is empty|No inventory was parsed|provided hosts list|Could not match supplied host pattern", w):
                msgs.append(msg(None, "warning", w, file=entry))
    return {"tool": "ansible-playbook --syntax-check", "target": entry, "status": status_of(msgs), "messages": msgs}


def terraform_validate(files, module_dir):
    tf = shutil.which("terraform")
    if not (module_dir / ".terraform").is_dir():
        return {"tool": "terraform validate", "target": module_dir.name, "status": "ok",
                "messages": [msg(None, "info", f"Skipped: run `terraform init` in {module_dir.name}/ once to enable full validation")]}
    with tempfile.TemporaryDirectory(prefix="hub-tf-") as d:
        write_tree([f for f in files if not f["path"].startswith(".terraform/")], d)
        os.symlink(module_dir / ".terraform", Path(d) / ".terraform")
        r = run([tf, "validate", "-no-color", "-json"], cwd=d, env={"TF_IN_AUTOMATION": "1"})
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return {"tool": "terraform validate", "target": module_dir.name, "status": "error",
                "messages": [msg(None, "error", (r.stderr or r.stdout).strip()[-800:])]}
    msgs = []
    for dg in data.get("diagnostics", []):
        rng = dg.get("range") or {}
        msgs.append(msg((rng.get("start") or {}).get("line"), "error" if dg.get("severity") == "error" else "warning",
                        dg.get("summary", "") + (f": {dg['detail']}" if dg.get("detail") else ""), file=rng.get("filename")))
    return {"tool": "terraform validate", "target": module_dir.name, "status": status_of(msgs), "messages": msgs}


ROLE = {"ansible": "playbook", "eda": "rulebook"}


def validate_automation(a, files, repo):
    """Check every stored file of an automation plus its entry point. a is a dict row."""
    t0 = time.time()
    results = [check_file(f["path"], f["content"], ROLE.get(a["type"]) if f["path"] == a["entry"] else None) for f in files]
    checks = []
    if not files:
        checks.append({"tool": "files", "target": a["dir"], "status": "warning",
                       "messages": [msg(None, "warning", "No files are stored for this automation. Sync it from the repository or upload them.")]})
    elif a["entry"] not in {f["path"] for f in files}:
        checks.append({"tool": "entry", "target": a["entry"], "status": "error",
                       "messages": [msg(None, "error", f"Entry file {a['entry']} is not among the stored files")]})
    elif a["type"] == "ansible" and shutil.which("ansible-playbook"):
        checks.append(ansible_syntax(files, a["entry"]))
    elif a["type"] == "terraform" and shutil.which("terraform"):
        checks.append(terraform_validate(files, (Path(repo) / a["dir"]).resolve()))
    counts = {"files": len(results), "ok": 0, "warning": 0, "error": 0, "skipped": 0}
    for r in results:
        counts[r["status"] if r["status"] in counts else "skipped"] += 1
    status = worst([r["status"] for r in results] + [c["status"] for c in checks]) if files else "warning"
    return {"automation_id": a["id"], "status": status, "checked": now(), "seconds": round(time.time() - t0, 2),
            "counts": counts, "files": results, "checks": checks}


# ------------------------------------------------------------ workflow test
def parse_vars(text):
    out = {}
    for line in str(text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line[1:]:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _block(text, start):
    """Text inside the {...} block whose opening brace is at start."""
    depth, i, in_str = 0, start, False
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
        i += 1
    return text[start + 1:]


def tf_variables(files):
    """{name: {required, sensitive, file, line}} for variables declared in .tf files."""
    out = {}
    for f in files:
        if not f["path"].endswith(".tf") or "/" in f["path"]:
            continue  # root module only
        c = f["content"]
        for m in re.finditer(r'^\s*variable\s+"([^"]+)"\s*\{', c, re.M):
            body = _block(c, m.end() - 1)
            out[m[1]] = {"required": not re.search(r"^\s*default\s*=", body, re.M),
                         "sensitive": bool(re.search(r"^\s*sensitive\s*=\s*true", body, re.M)),
                         "file": f["path"], "line": c.count("\n", 0, m.start()) + 1}
    return out


NEEDS = {"terraform": "terraform", "ansible": "ansible-playbook", "eda": "ansible-rulebook", "shell": "bash", "python": "python3"}


def local_tf_values(module_dir):
    """Names (never values) of variables set outside the hub: terraform.tfvars, *.auto.tfvars, TF_VAR_*."""
    names = {k[7:] for k in os.environ if k.startswith("TF_VAR_")}
    if module_dir and module_dir.is_dir():
        for p in [module_dir / "terraform.tfvars", *module_dir.glob("*.auto.tfvars")]:
            try:
                names.update(re.findall(r"^\s*([A-Za-z_][\w-]*)\s*=", p.read_text(errors="replace"), re.M))
            except OSError:
                pass
    return names


def node_readiness(a, node, files, validation, repo=None):
    """Problems that would stop this node from running: syntax, variables, inventory, tools."""
    issues = []
    if validation["status"] == "error":
        n = sum(1 for f in validation["files"] if f["status"] == "error") + sum(1 for c in validation["checks"] if c["status"] == "error")
        issues.append(msg(None, "error", f"Syntax check failed in {n} place(s); see the details below"))
    elif validation["status"] == "warning" and files:
        issues.append(msg(None, "warning", "Syntax check passed with warnings"))
    if not tools().get(NEEDS[a["type"]]):
        issues.append(msg(None, "warning", f"{NEEDS[a['type']]} is not installed on this machine, so the exported runner can't run this step here"))
    merged = {**parse_vars(a["vars"]), **parse_vars(node.get("vars"))}

    if a["type"] == "terraform":
        declared = tf_variables(files)
        elsewhere = local_tf_values((Path(repo) / a["dir"]).resolve() if repo else None)
        for name, info in declared.items():
            if info["required"] and name not in merged and name not in elsewhere:
                if info["sensitive"]:
                    issues.append(msg(None, "warning", f'Sensitive variable "{name}" has no value here: export TF_VAR_{name} before running'))
                else:
                    issues.append(msg(None, "error", f'Required variable "{name}" is not set ({info["file"]} line {info["line"]}). '
                                                     f'Add {name}=... to the node or catalog variables, or export TF_VAR_{name}'))
        if declared:
            for k in merged:
                if k not in declared:
                    issues.append(msg(None, "error", f'"{k}" is not a variable of this module; terraform rejects undeclared -var values'))

    # Hub inventories are checked in the browser, which holds their content; Terraform IPs only exist at run time.
    if a["type"] == "ansible" and not node.get("inventoryFrom") and not node.get("inventoryId"):
        cfg = next((f["content"] for f in files if f["path"] == "ansible.cfg"), "")
        m = re.search(r"^\s*inventory\s*=\s*(\S+)", cfg, re.M)
        inv_path = (m[1] if m else "inventory/hosts.ini").lstrip("./")
        inv = next((f for f in files if f["path"] == inv_path), None)
        if inv:
            hosts = hosts_in(*inventory_hosts(inv["content"]), a["target_group"] or None)
            if not hosts:
                where = f" in [{a['target_group']}]" if a["target_group"] else ""
                issues.append(msg(None, "warning", f"{inv_path} has no hosts{where}, so the playbook would match nothing. "
                                                   "Add hosts there, pick a hub inventory, or take the inventory from a Terraform node"))
            else:
                issues += limit_issues(node.get("limit"), inv["content"], inv_path)
        elif files:
            issues.append(msg(None, "info", f"Inventory {inv_path} is not stored, so its hosts could not be checked"))
    return issues
