/* Interactive teaching examples are initialized progressively; the article and
   static diagrams remain readable without JavaScript. */
'use strict';

// Original toy models; these are not Unreal measurements.
(() => {
  const NS='http://www.w3.org/2000/svg';
  const C={blue:'#65baff',green:'#6ce1b5',gold:'#ffd36f',muted:'#9eafc3',white:'#eef4ff',purple:'#c2a4ff'};
  function el(tag,attrs,parent,text){const n=document.createElementNS(NS,tag);for(const[k,v]of Object.entries(attrs||{}))n.setAttribute(k,v);if(text!==undefined)n.textContent=text;parent.appendChild(n);return n;}
  function label(s,x,y,text,color=C.white,size=19){return el('text',{x,y,fill:color,'font-size':size,'font-family':'system-ui, sans-serif'},s,text);}
  function line(s,x1,y1,x2,y2,color,width=2){return el('line',{x1,y1,x2,y2,stroke:color,'stroke-width':width},s);}
  function svgFor(host,title){const s=host.querySelector('svg');s.replaceChildren();el('title',{},s,title);return s;}
  function rng(seed){return()=>{seed=(Math.imul(1664525,seed)+1013904223)>>>0;return seed/4294967296;};}
  const sampler=document.querySelector('[data-demo="hemisphere"]');
  if(sampler){
    let seed=20261115;
    const draw=()=>{
      const count=Number(sampler.querySelector('input').value),s=svgFor(sampler,'Uniform and cosine sampling of the same constant-field diffuse response');
      const random=rng(seed),uniforms=Array.from({length:count},()=>[random(),random()]);let average=0;
      for(let mode=0;mode<2;mode++){
        const cx=mode?680:220,cy=270,r=150;
        label(s,cx-180,38,mode?'Cosine-weighted hemisphere':'Uniform hemisphere',mode?C.green:C.blue,21);
        el('ellipse',{cx,cy,rx:r,ry:35,fill:'none',stroke:C.muted},s);
        el('path',{d:`M ${cx-r} ${cy} A ${r} ${r} 0 0 1 ${cx+r} ${cy}`,fill:'none',stroke:C.muted},s);
        line(s,cx,cy,cx,cy-r-25,C.white);label(s,cx+8,cy-r-15,'n');
        for(const[u,v]of uniforms){
          const z=mode?Math.sqrt(1-u):u,radial=Math.sqrt(1-z*z),phi=2*Math.PI*v,x=cx+r*radial*Math.cos(phi),y=cy-r*z+35*radial*Math.sin(phi);
          line(s,cx,cy,x,y,mode?C.green:C.blue,.8);el('circle',{cx:x,cy:y,r:2.5,fill:mode?C.green:C.blue},s);
          if(!mode)average+=2*.6*2*z/count;
        }
        label(s,cx-170,330,mode?'sample value = ρL₀ = 1.2':'sample value = 2ρL₀ cos θ');
      }
      label(s,40,378,'Same incident field L₀ = 2 and diffuse reflectance ρ = 0.6.',C.gold,20);
      label(s,40,412,'Orthographic view of 3D directions; hidden endpoints remain visible.',C.muted,16);
      sampler.querySelector('output').textContent=`${count} directions per strategy · uniform estimate ${average.toFixed(4)} · cosine estimate 1.2000 · exact 1.2000`;
    };
    sampler.querySelector('input').addEventListener('input',draw);sampler.querySelector('button').addEventListener('click',()=>{seed=(seed+1)>>>0;draw();});draw();
  }
  const cache=document.querySelector('[data-demo="cache"]');
  if(cache){
    const draw=()=>{
      const iteration=Number(cache.querySelector('input').value),approximate=cache.querySelector('select').value==='approximate',k=approximate?.25:.3,values=[[1,0]];
      for(let i=0;i<12;i++){const[a,b]=values[values.length-1];values.push([1+.2*b,k*a]);}
      const[a,b]=values[iteration],exactB=.3/(1-.2*.3),fixedB=k/(1-.2*k),s=svgFor(cache,'Two-patch iteration separates convergence from modeling error');
      for(const[x,name,value,color]of[[50,'Sunlit patch A',a,C.gold],[510,'Shaded patch B',b,C.green]]){
        el('rect',{x,y:65,width:340,height:140,rx:8,fill:'#24384e',stroke:color},s);label(s,x+22,100,name,color,23);label(s,x+22,150,`radiance = ${value.toFixed(5)}`,C.white,20);
      }
      label(s,52,245,'A(new) = 1 + 0.2 × B(old)',C.gold);label(s,510,245,`B(new) = ${k} × A(old)`,C.green);
      line(s,60,415,845,415,C.muted);line(s,60,415,60,280,C.muted);const py=v=>415-v*350;
      line(s,60,py(exactB),845,py(exactB),C.purple);label(s,510,py(exactB)-12,'ideal fixed point: 0.31915',C.purple,17);
      const points=values.slice(0,iteration+1).map((v,i)=>`${60+i*65},${py(v[1])}`).join(' ');
      el('polyline',{points,fill:'none',stroke:C.green,'stroke-width':3},s);
      for(let i=0;i<=iteration;i++)el('circle',{cx:60+i*65,cy:py(values[i][1]),r:4,fill:C.green},s);
      label(s,60,450,'0',C.muted,16);label(s,770,450,'12 updates',C.muted,16);
      cache.querySelector('output').textContent=`Update ${iteration} · B = ${b.toFixed(5)} · chosen model converges to ${fixedB.toFixed(5)} · ideal target ${exactB.toFixed(5)}`;
    };
    cache.querySelector('input').addEventListener('input',draw);cache.querySelector('select').addEventListener('change',draw);draw();
  }
  const history=document.querySelector('[data-demo="history"]');
  if(history){
    const draw=()=>{
      const alpha=Number(history.querySelector('input').value)/100,s=svgFor(history,'History responds gradually to a sudden lighting change'),x=i=>70+i*24,y=v=>335-v*240;
      line(s,70,335,810,335,C.muted);line(s,70,335,70,70,C.muted);line(s,70,y(1),810,y(1),C.gold);label(s,480,77,'new lighting = 1 immediately',C.gold,18);
      const points=Array.from({length:31},(_,i)=>`${x(i)},${y(1-Math.pow(1-alpha,i))}`).join(' ');el('polyline',{points,fill:'none',stroke:C.green,'stroke-width':4},s);
      for(const i of[0,1,5,10,20,30]){const value=1-Math.pow(1-alpha,i);el('circle',{cx:x(i),cy:y(value),r:4,fill:C.green},s);label(s,x(i)-8,363,String(i),C.muted,16);}
      label(s,70,405,'Updates after the change; valid history, no rejection or clamps',C.muted,17);label(s,70,38,'H(new) = (1 − a) × H(old) + a × current',C.white,23);
      const n95=alpha===1?1:Math.ceil(Math.log(.05)/Math.log(1-alpha)),varianceRatio=alpha/(2-alpha);
      history.querySelector('output').textContent=`a = ${alpha.toFixed(2)} · after 10 updates ${(1-Math.pow(1-alpha,10)).toFixed(4)} · 95% response in ${n95} updates · stationary noise variance ×${varianceRatio.toFixed(3)}`;
    };
    history.querySelector('input').addEventListener('input',draw);draw();
  }
})();
