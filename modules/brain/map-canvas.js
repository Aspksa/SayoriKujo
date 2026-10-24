'use strict';
window.SayoriMapCanvas = class {
  constructor(viewport,space,stage,draw){
    this.viewport=viewport;this.space=space;this.stage=stage;this.draw=draw;this.zoom=1;this.baseWidth=0;this.pinch=null;
    this.viewport.addEventListener('wheel',event=>{
      if(!event.ctrlKey&&!event.metaKey)return;
      event.preventDefault();
      const rect=this.viewport.getBoundingClientRect();
      const delta=event.deltaY*(event.deltaMode===1?16:event.deltaMode===2?this.viewport.clientHeight:1);
      this.setZoom(this.zoom*Math.exp(-delta*.002),{x:event.clientX-rect.left,y:event.clientY-rect.top});
    },{passive:false});
    this.viewport.addEventListener('touchstart',event=>{
      if(event.touches.length!==2){this.pinch=null;return;}
      event.preventDefault();const [a,b]=event.touches;
      const rect=this.viewport.getBoundingClientRect(),center={x:(a.clientX+b.clientX)/2-rect.left,y:(a.clientY+b.clientY)/2-rect.top};
      this.pinch={distance:Math.hypot(a.clientX-b.clientX,a.clientY-b.clientY),zoom:this.zoom,
        logical:{x:(this.viewport.scrollLeft+center.x)/this.zoom,y:(this.viewport.scrollTop+center.y)/this.zoom}};
    },{passive:false});
    this.viewport.addEventListener('touchmove',event=>{
      if(!this.pinch||event.touches.length!==2)return;
      event.preventDefault();const [a,b]=event.touches,rect=this.viewport.getBoundingClientRect();
      const center={x:(a.clientX+b.clientX)/2-rect.left,y:(a.clientY+b.clientY)/2-rect.top};
      this.setZoom(this.pinch.zoom*Math.hypot(a.clientX-b.clientX,a.clientY-b.clientY)/Math.max(1,this.pinch.distance),center,this.pinch.logical);
    },{passive:false});
    const end=()=>{this.pinch=null;};this.viewport.addEventListener('touchend',end);this.viewport.addEventListener('touchcancel',end);
    this.observer=new ResizeObserver(()=>{
      const width=this.viewport.clientWidth;if(!width)return;
      if(Math.abs(width-this.baseWidth)>.5){const saved=this.capture();this.baseWidth=width;this.sync();this.restore(saved);}
      else this.sync();
    });this.observer.observe(this.viewport);this.observer.observe(this.stage);
  }
  capture(){
    const rect=this.viewport.getBoundingClientRect();let anchor=null,best=Infinity;
    for(const card of this.stage.querySelectorAll('.map-node')){
      const box=card.getBoundingClientRect();
      if(box.bottom<=rect.top||box.top>=rect.bottom||box.right<=rect.left||box.left>=rect.right)continue;
      const distance=Math.abs(box.top-rect.top)+Math.abs(box.left-rect.left);
      if(distance<best){best=distance;anchor={id:card.dataset.nodeId,x:box.left-rect.left,y:box.top-rect.top};}
    }
    return {left:this.viewport.scrollLeft,top:this.viewport.scrollTop,anchor};
  }
  restore(saved){
    if(!saved)return;
    this.sync();this.viewport.scrollLeft=saved.left;this.viewport.scrollTop=saved.top;
    if(saved.anchor){const card=[...this.stage.querySelectorAll('.map-node')].find(card=>card.dataset.nodeId===saved.anchor.id);
      if(card){const a=card.getBoundingClientRect(),b=this.viewport.getBoundingClientRect();
        this.viewport.scrollLeft+=a.left-b.left-saved.anchor.x;this.viewport.scrollTop+=a.top-b.top-saved.anchor.y;}}
    requestAnimationFrame(this.draw);
  }
  sync(){
    const width=this.viewport.clientWidth;if(!width)return;
    this.baseWidth=width;this.stage.style.width=width+'px';this.stage.style.transform='scale('+this.zoom+')';
    this.space.style.width=Math.max(width,width*this.zoom)+'px';this.space.style.height=Math.ceil(this.stage.offsetHeight*this.zoom)+'px';
    document.getElementById('map-zoom-value').textContent=Math.round(this.zoom*100)+'%';
    document.getElementById('map-zoom-out').disabled=this.zoom<=.5001;
    document.getElementById('map-zoom-in').disabled=this.zoom>=1.9999;
    requestAnimationFrame(this.draw);
  }
  setZoom(value,point=null,logical=null){
    const next=Math.max(.5,Math.min(2,Number(value)));if(!Number.isFinite(next))return;
    const anchor=point||{x:this.viewport.clientWidth/2,y:this.viewport.clientHeight/2};
    const position=logical||{x:(this.viewport.scrollLeft+anchor.x)/this.zoom,y:(this.viewport.scrollTop+anchor.y)/this.zoom};
    this.zoom=next;this.sync();this.viewport.scrollLeft=position.x*next-anchor.x;this.viewport.scrollTop=position.y*next-anchor.y;
  }
};
