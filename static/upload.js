// ------ Batch upload: the browser sends the file to the server in batches of lines ------
const form = document.querySelector("#upload-form");

form.addEventListener("submit", async (event) => {
   const file = form.querySelector('input[type="file"]').files[0];
   if (!file) return;          // no file: normal submission, the server shows its error
   event.preventDefault();     // from here on, JavaScript handles the upload
   
   const text = await file.text();
   const lines = text.split(/\r?\n/);
   
   // ------ Show the progress bar and block a second submission ------
   const progress = document.querySelector("#upload-progress");
   const progressText = document.querySelector("#progress-text");
   const bar = progress.querySelector(".progress-bar");
   const fill = progress.querySelector(".fill");
   progress.hidden = false;
   const button = form.querySelector('button[type="submit"]');
   button.disabled = true;

   const data = new FormData(form);   // csrf_token, name, description… and the file
   data.delete("file");
   let response;
   try {
      response = await fetch("/libraries/start", { method: "POST", body: data });
   } catch (error) {
      progressText.textContent = "Could not reach the server. Check your connection.";
      button.disabled = false;
      return;
   }
   if (!response.ok) { form.submit(); return; }   // invalid form: let the server show the errors
   const { id } = await response.json();
   
   // ------ Send the lines in batches of 100 ------
   const BATCH_SIZE = 100;
   for (let i = 0; i < lines.length; i += BATCH_SIZE) {
      const batch = lines.slice(i, i+BATCH_SIZE);
      const batchData = new FormData();
      batchData.append("csrf_token", data.get("csrf_token"));
      batchData.append("start", i+1);
      batchData.append("text", batch.join("\n"));
      let batchResponse;
      try{
         batchResponse = await fetch(`/libraries/${id}/batch`, { method: "POST", body: batchData });
      }catch (error){
         progressText.textContent = `Batch starting at line ${i+1} failed. Lines before it were saved.`;
         button.disabled = false;
         return;
      }
      
      if(!batchResponse.ok){
         progressText.textContent=`Batch starting at line ${i+1} failed. Lines before it were saved.`;
         button.disabled = false;
         return;
      }
      // ------ Move the bar ------
      const done = Math.min(i + BATCH_SIZE, lines.length);
      const percent = Math.round((done/lines.length)*100);
      fill.style.width = `${percent}%`;
      bar.setAttribute("aria-valuenow", percent);
      progressText.textContent = `${done} of ${lines.length} lines`;
   }
   
   // ------ All batches saved: the summary is waiting in the session ------
   window.location.href = "/libraries";
   
});