def schedule(fn, arg):
    try:
        fn(arg)
    except Exception:
        pass

def const(x):
    return x
