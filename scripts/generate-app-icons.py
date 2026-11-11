"""Render the existing Workdesk line mark into deterministic PNG app icons (stdlib)."""
from pathlib import Path
import struct
import zlib

SEGMENTS = [(6,20,16,8), (16,8,26,20), (26,20,16,26), (16,26,6,20), (12,20,16,15), (16,15,20,20)]
def distance(x,y,segment):
    ax,ay,bx,by=segment
    dx,dy=bx-ax,by-ay
    t=max(0,min(1,((x-ax)*dx+(y-ay)*dy)/(dx*dx+dy*dy)))
    return ((x-ax-t*dx)**2+(y-ay-t*dy)**2)**.5

def chunk(kind,data):
    return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)

for size in (192,512):
    raw=bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            # Keep the mark within the central safe area for maskable app icons.
            px=(x/size-.5)*44+16
            py=(y/size-.5)*44+16
            coverage=max(0,min(1,(1.35-min(distance(px,py,line) for line in SEGMENTS))*size/44))
            raw.extend(round(a+(b-a)*coverage) for a,b in zip((11,17,32),(97,219,191)))
    data=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',size,size,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw,9))+chunk(b'IEND',b'')
    (Path(__file__).resolve().parents[1]/'static'/f'icon-{size}.png').write_bytes(data)
