from flask import Flask,request,jsonify,render_template_string
from openai import OpenAI
import sqlite3,markdown

app=Flask(__name__)

DB="chat.db"


def db():
    return sqlite3.connect(DB)


def init():
    c=db()
    c.execute("""
    create table if not exists config(
    id integer primary key,
    key text,
    base text,
    model text)
    """)

    c.execute("""
    create table if not exists chat(
    id integer primary key,
    role text,
    content text)
    """)

    c.commit()


init()


HTML="""

<!doctype html>

<html>

<head>

<title>AI Sandbox</title>

<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>

<link href="https://cdn.jsdelivr.net/npm/highlight.js/styles/github-dark.min.css" rel="stylesheet">

<script src="https://cdn.jsdelivr.net/npm/highlight.js/lib/common.min.js"></script>


<style>

body{
margin:0;
font-family:Arial;
display:flex;
height:100vh;
background:#343541;
color:white;
}

#left{
width:260px;
background:#202123;
padding:15px;
}

#chat{
flex:1;
display:flex;
flex-direction:column;
}

#messages{
flex:1;
overflow:auto;
padding:30px;
}

.msg{
padding:15px;
margin:10px;
border-radius:8px;
line-height:1.6;
}

.user{
background:#444654;
}

.ai{
background:#343541;
}

#input{
display:flex;
padding:15px;
}

textarea{
flex:1;
height:70px;
font-size:16px;
}

button{
width:100px;
}


input{
width:100%;
margin-bottom:10px;
}

</style>

</head>


<body>


<div id="left">

<h3>AI设置</h3>

API KEY

<input id="key">

Base URL

<input id="base" value="https://api.openai.com/v1">


模型

<input id="model" value="gpt-5">


<button onclick="save()">保存</button>


<hr>

<h3>历史</h3>

<div id="history"></div>


</div>



<div id="chat">


<div id="messages"></div>


<div id="input">

<textarea id="text"></textarea>

<button onclick="send()">发送</button>

</div>


</div>



<script>


async function save(){

await fetch('/config',{
method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({

key:key.value,
base:base.value,
model:model.value

})

})

alert("保存成功")

}



async function load(){

let r=await fetch('/history')

let d=await r.json()

messages.innerHTML=""

d.forEach(x=>{

add(x.role,x.content)

})

}


function add(role,text){

let div=document.createElement("div")

div.className="msg "+role

div.innerHTML=marked.parse(text)

messages.appendChild(div)

messages.scrollTop=messages.scrollHeight

document.querySelectorAll("pre code")
.forEach(e=>hljs.highlightElement(e))

}



async function send(){

let t=text.value

if(!t)return

text.value=""

add("user",t)


let r=await fetch('/chat',{

method:'POST',

headers:{
'Content-Type':'application/json'
},

body:JSON.stringify({

message:t

})

})


let d=await r.json()

add("ai",d.answer)

}


load()


</script>


</body>

</html>

"""


@app.route("/")
def index():
    return render_template_string(HTML)



@app.route("/config",methods=["POST"])
def config():

    d=request.json

    c=db()

    c.execute("delete from config")

    c.execute(
    "insert into config values(1,?,?,?)",
    (
    d["key"],
    d["base"],
    d["model"]
    ))

    c.commit()

    return "ok"



@app.route("/chat",methods=["POST"])
def chat():

    msg=request.json["message"]

    c=db()

    cfg=c.execute(
    "select * from config"
    ).fetchone()


    if not cfg:
        return jsonify(
        answer="请先设置API"
        )


    _,key,base,model=cfg


    client=OpenAI(
    api_key=key,
    base_url=base
    )


    history=c.execute(
    "select role,content from chat order by id"
    ).fetchall()


    messages=[
    {
    "role":"system",
    "content":"你是一个智能助手"
    }
    ]


    for r,t in history:

        messages.append(
        {
        "role":r,
        "content":t
        })


    messages.append(
    {
    "role":"user",
    "content":msg
    })


    try:

        res=client.chat.completions.create(

        model=model,

        messages=messages

        )


        answer=res.choices[0].message.content


    except Exception as e:

        answer="API错误:\n"+str(e)



    c.execute(
    "insert into chat(role,content) values(?,?)",
    ("user",msg)
    )


    c.execute(
    "insert into chat(role,content) values(?,?)",
    ("ai",answer)
    )


    c.commit()


    return jsonify(
    answer=answer
    )




@app.route("/history")
def history():

    c=db()

    rows=c.execute(
    "select role,content from chat order by id"
    ).fetchall()

    return jsonify(
    [
    {
    "role":r,
    "content":t
    }
    for r,t in rows
    ])




app.run(
host="127.0.0.1",
port=5000
)
