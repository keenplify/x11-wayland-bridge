#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(void){
 Display*d=XOpenDisplay(NULL);if(!d)return 1;
 Window root=DefaultRootWindow(d),parent,*children=NULL,selected=0;unsigned n=0;
 if(XQueryTree(d,root,&root,&parent,&children,&n)){
  for(unsigned i=0;i<n;i++){char*name=NULL;XFetchName(d,children[i],&name);
   if(name&&strstr(name,"Screen Share - "))selected=children[i];
   if(name)XFree(name);
  }if(children)XFree(children);
 }
 if(!selected){fprintf(stderr,"Select a window or screen in the video bridge tray first.\n");return 2;}
 int x,y;unsigned w,h,border,depth;
 if(!XGetGeometry(d,selected,&root,&x,&y,&w,&h,&border,&depth))return 3;
 XImage*im=XGetImage(d,selected,0,0,w,h,AllPlanes,ZPixmap);if(!im)return 4;
 unsigned char*row=malloc(w*3);if(!row)return 5;
 printf("P6\n%u %u\n255\n",w,h);
 for(unsigned y=0;y<h;y++){for(unsigned x=0;x<w;x++){unsigned long v=XGetPixel(im,x,y);row[x*3]=v>>16;row[x*3+1]=v>>8;row[x*3+2]=v;}fwrite(row,3,w,stdout);}
 free(row);XDestroyImage(im);XCloseDisplay(d);return 0;
}
