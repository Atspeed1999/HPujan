import { useEffect } from "react";

function App() {
  useEffect(() => {
    window.location.replace("/landing.html");
  }, []);

  return null;
}

export default App;
