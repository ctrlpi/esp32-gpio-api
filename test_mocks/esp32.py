def mcu_temperature():
    return 45.2

def raw_temperature():
    return 113.4

HEAP_DATA = 4

def idf_heap_info(caps):
    # (total, free, largest free block, low-water mark) per region: a healthy classic.
    return [(237680, 24000, 21504, 15000)]
